"""Build, verify, QA, and publish the private read-only control panel."""

from __future__ import annotations

import argparse
import base64
import binascii
from collections import Counter, defaultdict
import copy
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
import gzip
import hashlib
from html import escape as html_escape
from html.parser import HTMLParser
from importlib import import_module
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import zlib
from typing import TYPE_CHECKING, Any, Collection, Final, Mapping, Sequence, cast

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    CurrentPointer,
    FreshnessPolicy,
    GenerationConflictError,
    GenerationBundle,
    GenerationManifest,
    ManifestValidationError,
    PointerIntegrityError,
    SourceArtifact,
    VerificationStatus,
    hash_file,
    publish_generation,
    read_current,
    read_generation_bundle,
    stage_generation,
)
from quant_engine.core.contracts import canonical_json_bytes
from quant_engine.core import inventory_identity
import sleeve_disclosures
from portfolio_private_stage import validate_private_stage_target
from portfolio_invested_capital import (
    CapitalValidationError,
    governed_hwm_input_policy,
    resolve_financial_input_authority,
    verify_governed_capital_result,
)
from quant_engine.portfolio_ledger import (
    HWM_STATUS_CODE_VERSION,
    HWM_STATUS_SEMANTICS,
    HwmCoverageError,
    HwmInputsContractError,
    compute_full_systematic_hwm_index,
    decimal_text,
    governed_hwm_coverage,
    hwm_policy_exclusions,
    validate_hwm_inputs_envelope,
    validate_as_held_hwm_receipt_generation_binding,
)
from quant_engine.portfolio_ledger.hwm_governed_execution import (
    verify_governed_hwm_result,
)

if TYPE_CHECKING:
    from account_strategy_manifest import (
        ManifestError,
        build_manifest_candidate,
        validate_manifest_schema,
    )
else:
    try:
        _manifest_module = import_module("tools.account_strategy_manifest")
    except ModuleNotFoundError as exc:  # direct `python tools/...` execution
        if exc.name != "tools":
            raise
        _manifest_module = import_module("account_strategy_manifest")
    ManifestError = _manifest_module.ManifestError
    build_manifest_candidate = _manifest_module.build_manifest_candidate
    validate_manifest_schema = _manifest_module.validate_manifest_schema

if TYPE_CHECKING:
    from portfolio_performance_tab import (
        build_performance_tab,
        verify_performance_tab,
    )
else:
    try:
        _performance_tab_module = import_module("tools.portfolio_performance_tab")
    except ModuleNotFoundError as exc:  # direct `python tools/...` execution
        if exc.name != "tools":
            raise
        _performance_tab_module = import_module("portfolio_performance_tab")
    build_performance_tab = _performance_tab_module.build_performance_tab
    verify_performance_tab = _performance_tab_module.verify_performance_tab

ACCOUNT_MANIFEST_CALCULATION_CODE: Final = Path(
    build_manifest_candidate.__code__.co_filename
).resolve()


PORTFOLIO_TOTALS_KEYS: Final = (
    "portfolio_twr_cumulative",
    "portfolio_mwr_annualised",
    "capital_reconciliation",
)
PANEL_SCHEMA_VERSION: Final = 24
# BIL DBC GLD HYG IEF IEFA IEMG ITOT UUP plus SPY and SPXL (weekly returns CORE_BENCHMARKS).
CORE_BENCHMARK_COUNT: Final = 11
# A panel built from a version-3 report (custody cash dated by its admission coverage)
# is version 25; every other panel is 24.  Versions 22 and 23 (the same split before
# the position snapshot split and the governed HWM candidate) stay readable.
CUSTODY_COVERAGE_PANEL_SCHEMA_VERSION: Final = 25
BUILDER_VERSION: Final = "2.13.0"
CURRENT_REQUIRED_STRATEGY_IDS: Final = frozenset(
    {
        "grizzlybears-vtma",
        "p123-ai-factor",
        "p123-eu",
        "qsi",
        "robuxio-crypto",
        "tt-portfolio",
        "volatis-strategy",
    }
)
CURRENT_EXCLUDED_STRATEGY_IDS: Final = frozenset(
    {"echotrade-growth-pivots", "composer-holy-grail"}
)
CURRENT_PRODUCT_STRATEGY_IDS: Final = (
    CURRENT_REQUIRED_STRATEGY_IDS | CURRENT_EXCLUDED_STRATEGY_IDS
)
CURRENT_PRODUCT_EPOCH_COUNT: Final = 13
MANIFEST_AUTHORITY_FINANCIAL: Final = "INDEPENDENT_EXACT_REBUILD"
MANIFEST_AUTHORITY_INTERNAL: Final = "INTERNAL_VALIDATION_ONLY_NON_RELEASEABLE"
MANIFEST_AUTHORITY_ABSENT: Final = "NOT_SUPPLIED_NON_RELEASEABLE"
MANIFEST_AUTHORITY_VERIFIED_BUNDLE: Final = "VERIFIED_GENERATION_BUNDLE_EXACT_BYTES"
STRATEGY_EVIDENCE_MODES: Final = ("LIVE_ONLY", "LIVE_PLUS_BACKTEST")
DEPENDENCE_HORIZONS: Final = ("5", "21", "22", "63", "126", "130", "252", "260")
DEPENDENCE_DRAWDOWN_REGIMES: Final = (
    "either_strategy_underwater",
    "both_strategies_underwater",
)
SOURCE_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "portfolio-control-panel-source-v1.json"
)
FINANCIAL_MVP_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "portfolio-financial-mvp-source-v1.json"
)
PANEL_DATA_SCHEMA = (
    Path(__file__).resolve().parents[1]
    / "schemas"
    / "portfolio"
    / "control-panel-data.schema.json"
)
ACCOUNT_MANIFEST_SCHEMA = (
    Path(__file__).resolve().parents[1]
    / "schemas"
    / "portfolio"
    / "account-strategy-manifest.schema.json"
)
ACCOUNT_MANIFEST_INDEPENDENT_VERIFICATION_SCHEMA = (
    Path(__file__).resolve().parents[1]
    / "schemas"
    / "portfolio"
    / "account-manifest-independent-verification.schema.json"
)
HWM_POLICY = (
    Path(__file__).resolve().parents[1] / "contracts" / "portfolio-hwm-policy-v3.json"
)
DEPENDENCE_POLICY = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "portfolio-dependence-policy-v1.json"
)
_PORTABLE_HWM_POLICY: Final = (
    Path(__file__).resolve().parents[1] / "contracts" / "portfolio-hwm-policy-v4.json"
)
_PORTABLE_HWM_PRODUCT_CONTRACT: Final = FINANCIAL_MVP_CONTRACT
_PORTABLE_HWM_PERFORMANCE_POLICY: Final = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "portfolio-performance-policy-v1.json"
)
ECONOMIC_DRIVER_CLUSTERS = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "portfolio-economic-driver-clusters-v1.json"
)
STRATEGY_UNDERWRITING_POLICY = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "portfolio-strategy-underwriting-v1.json"
)
DEFAULT_TEMPLATE_ROOT = (
    Path(__file__).resolve().parents[1] / "apps" / "portfolio-control-panel"
)
FLOW_CLASSIFICATION_POLICY = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "account-strategy-flow-classification-policy-v1.json"
)
_GOVERNED_MEMBERSHIP_CENSUS: Final[Path] = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "portfolio-financial-mvp-source-v1.json"
)
PANEL_FILES: Final = (
    "index.html",
    "styles.css",
    "analytics.css",
    "app.js",
    "data/dashboard_data.json",
    "data/dashboard_data.js",
    "data/freshness-health.json",
)
REPORT_DOWNLOAD: Final = "downloads/portfolio-report-data.json"
WORKBOOK_DOWNLOAD: Final = "downloads/portfolio-report.xlsx"
_DOWNLOAD_METADATA: Final = {
    REPORT_DOWNLOAD: (
        "portfolio-report-data.json",
        "application/json",
        "download-payload-report-json",
    ),
    WORKBOOK_DOWNLOAD: (
        "portfolio-report.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "download-payload-report-xlsx",
    ),
}
_DOWNLOAD_BASE64_CHUNK_SIZE: Final = 65_536
# Must equal `EMBEDDED_DOWNLOAD_MAX_CHUNKS` in apps/portfolio-control-panel/app.js.
# A test pins the two together. The renderer decodes chunk by chunk and never
# joins them into one string, so this is a sanity bound on the embedded payload
# (about 512 MiB of base64), not a V8 string limit.
_DOWNLOAD_MAX_BASE64_CHUNKS: Final = 8191
# The report JSON is embedded gzip-compressed: it is ~10x smaller (332 MB ->
# 34 MB) and the page inflates it with `DecompressionStream` on click, so the
# direct `file://` route needs no sibling file and no network. The workbook is
# an .xlsx, already a zip container, and stays byte-identical.
_DOWNLOAD_ENCODINGS: Final = {REPORT_DOWNLOAD: "gzip", WORKBOOK_DOWNLOAD: "identity"}
_DOWNLOAD_GZIP_LEVEL: Final = 6
# Build-time size budget for the page that carries the downloads. index.html
# was 444 MB with the uncompressed report embedded; compressed it is ~47 MB.
_INDEX_HTML_MAX_BYTES: Final = 96 * 1024 * 1024
# `data/dashboard_data.js` carries the panel payload as gzip parts that are
# byte-exact slices of `data/dashboard_data.json` (head + lazy + tail in order).
# The LIVE_PLUS_BACKTEST lens array is ~73% of the payload and is parsed only
# when the viewer opens that mode. A part is inflated into one JS string, and
# V8's MAX_STRING_LENGTH is 536870888, so a part may not exceed this raw size.
_PANEL_LAZY_POINTER: Final = ("decomposition_lenses", "modes", "LIVE_PLUS_BACKTEST")
_PANEL_PART_MAX_RAW_BYTES: Final = 400 * 1024 * 1024
_PANEL_SCRIPT_MAX_BYTES: Final = 160 * 1024 * 1024
_PANEL_SCRIPT_GLOBAL: Final = "PORTFOLIO_DATA_PARTS"
_DOWNLOAD_PLACEHOLDER: Final = b"<!-- REPORT_DOWNLOADS -->"
_ALLOWED_FRONTEND_REFERENCES: Final = (
    ("link", "href", "styles.css"),
    ("link", "href", "analytics.css"),
    ("a", "href", "#main-content"),
    ("a", "href", "../../index.html"),
    ("script", "src", "data/dashboard_data.js"),
    ("script", "src", "app.js"),
)
_RESOURCE_ATTRIBUTES: Final = frozenset(
    {
        "action",
        "background",
        "cite",
        "data",
        "formaction",
        "href",
        "longdesc",
        "manifest",
        "ping",
        "poster",
        "src",
        "srcset",
    }
)
DRIVER_MAP_PANEL_FILE: Final = "data/candidate-driver-map.json"
DRIVER_MAP_CLUSTER_IDS: Final = frozenset(
    {
        "TACTICAL_TIMING_MULTI_ASSET",
        "US_SYSTEMATIC_EQUITY",
        "EUROPE_SYSTEMATIC_EQUITY",
        "CRYPTO_DIRECTIONAL",
        "DE_MINIMIS_OPERATIONAL",
    }
)
VIEWS: Final = (
    "command",
    "capital",
    "risk",
    "signals",
    "attribution",
    "dependence",
    "weekly",
    "candidates",
    "data",
)
_QA_VIEWPORTS: Final = ("desktop", "tablet", "mobile")
CAPTURES: Final = tuple(
    f"{viewport}-{view}" for viewport in _QA_VIEWPORTS for view in VIEWS
) + ("direct-file",)
LINT_CAPTURES: Final = tuple(
    f"{viewport}-{view}" for viewport in _QA_VIEWPORTS for view in VIEWS
)
DECOMPOSITION_LENS_SOURCES: Final = {
    "europe-economic": ("ECONOMIC_FACTOR", "European economic factors"),
    "multi-asset-style": ("ETF_STYLE", "Multi-asset style"),
    "us-economic": ("ECONOMIC_FACTOR", "U.S. economic factors"),
    "us-equity-style": ("ETF_STYLE", "U.S. equity style"),
    "us-industry": ("RETURN_INDUSTRY", "U.S. 49-industry returns"),
    "us-sector": ("RETURN_SECTOR", "U.S. sector returns"),
}
DECOMPOSITION_LENS_GROUPS: Final = (
    (
        "ECONOMIC_FACTOR",
        "Economic factors",
        ("us-economic", "europe-economic"),
        "EXCESS_AND_LONG_SHORT_FACTOR_RETURN",
    ),
    (
        "ETF_STYLE",
        "ETF style",
        ("us-equity-style", "multi-asset-style"),
        "TOTAL_RETURN_ETF_PROXY",
    ),
    (
        "RETURN_SECTOR",
        "Sector returns",
        ("us-sector",),
        "TOTAL_RETURN_ETF_PROXY",
    ),
    (
        "RETURN_INDUSTRY",
        "Industry returns",
        ("us-industry",),
        "TOTAL_RETURN_INDUSTRY_PORTFOLIO",
    ),
)


class ControlPanelValidationError(ValueError):
    """An input or generated control-panel artifact violates its contract."""


def _css_has_dynamic_resource(value: str) -> bool:
    # CSS escapes can spell resource-bearing tokens without their literal text.
    # The governed templates do not need escapes, so reject them fail closed.
    if "\\" in value:
        return True
    without_comments = re.sub(r"/\*.*?\*/", "", value, flags=re.S)
    return bool(re.search(r"(?:\burl\s*\(|@import\b)", without_comments, re.I))


class _FrontendReferenceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[tuple[str, str, str]] = []
        self.inline_style_url = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.lower()
        for raw_name, raw_value in attrs:
            name = raw_name.lower()
            value = raw_value or ""
            if name in _RESOURCE_ATTRIBUTES:
                self.references.append((normalized_tag, name, value))
            if name == "style" and _css_has_dynamic_resource(value):
                self.inline_style_url = True


def _closed_frontend_resources(candidate_dir: Path) -> tuple[bool, object]:
    try:
        paths = {
            name: _filesystem_path(candidate_dir / name)
            for name in ("index.html", "styles.css", "analytics.css")
        }
        if any(path.is_symlink() or not path.is_file() for path in paths.values()):
            return False, "required frontend file is missing or linked"
        html = paths["index.html"].read_text(encoding="utf-8")
        styles = "\n".join(
            paths[name].read_text(encoding="utf-8")
            for name in ("styles.css", "analytics.css")
        )
    except (OSError, UnicodeDecodeError) as exc:
        return False, f"frontend resource could not be read: {exc}"
    parser = _FrontendReferenceParser()
    parser.feed(html)
    actual = Counter(parser.references)
    expected = Counter(_ALLOWED_FRONTEND_REFERENCES)
    for relative in (REPORT_DOWNLOAD, WORKBOOK_DOWNLOAD):
        # A download is offered either as a file beside the page or as base64
        # payload elements inside it. The workbook is only ever the latter, so
        # presence on disk no longer decides whether its link is expected.
        payload_prefix = _DOWNLOAD_METADATA[relative][2]
        offered = _filesystem_path(candidate_dir / relative).is_file() or (
            f'data-download-payload-prefix="{payload_prefix}"' in html
        )
        if offered:
            expected[("a", "href", relative)] += 1
    css_has_dynamic_resource = _css_has_dynamic_resource(styles)
    is_closed = (
        actual == expected
        and not parser.inline_style_url
        and not css_has_dynamic_resource
    )
    return is_closed, {
        "actual_references": sorted(parser.references),
        "allowed_references": sorted(expected.elements()),
        "css_dynamic_resource": css_has_dynamic_resource,
        "inline_style_url": parser.inline_style_url,
    }


def _object(value: object, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ControlPanelValidationError(f"{field_name} must be an object")
    return value


def _objects(value: object, field_name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ControlPanelValidationError(f"{field_name} must be an array")
    return [_object(item, field_name) for item in value]


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ControlPanelValidationError(f"{field_name} must be non-empty text")
    return value.strip()


def _is_link_or_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError as exc:
        raise ControlPanelValidationError(
            f"authorization path is unavailable: {path}"
        ) from exc
    return path.is_symlink() or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _authorization_directory_identity(path: Path, field_name: str) -> dict[str, Any]:
    if _is_link_or_reparse_point(path) or not path.is_dir():
        raise ControlPanelValidationError(f"{field_name} is unavailable or linked")
    rows: list[dict[str, Any]] = []
    for item in sorted(path.rglob("*")):
        if _is_link_or_reparse_point(item):
            raise ControlPanelValidationError(f"{field_name} contains a link: {item}")
        if item.is_file():
            content = item.read_bytes()
            rows.append(
                {
                    "path": item.relative_to(path).as_posix(),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size_bytes": len(content),
                }
            )
    return inventory_identity(rows)


def _validated_coordinated_release_receipt(
    receipt_path: Path | None,
    output_root: Path,
    candidate_dir: Path,
    qa_dir: Path,
) -> tuple[str, str]:
    if receipt_path is None:
        raise ControlPanelValidationError("coordinated release receipt is required")
    content, receipt_sha256 = _read_snapshot(
        receipt_path, "coordinated release receipt"
    )
    try:
        receipt = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ControlPanelValidationError(
            "coordinated release receipt is not strict JSON"
        ) from exc
    receipt = _object(receipt, "coordinated release receipt")
    if canonical_json_bytes(receipt) != content:
        raise ControlPanelValidationError(
            "coordinated release receipt is not canonical"
        )
    expected_keys = {
        "schema_version",
        "operation",
        "state",
        "coordinator",
        "release_id",
        "release_commit",
        "output_root",
        "candidate",
        "qa",
    }
    if set(receipt) != expected_keys:
        raise ControlPanelValidationError(
            "coordinated release receipt root keys differ"
        )
    if type(receipt.get("schema_version")) is not int or receipt["schema_version"] != 1:
        raise ControlPanelValidationError("coordinated release receipt schema differs")
    if receipt.get("operation") != "PUBLISH_PANEL_CURRENT":
        raise ControlPanelValidationError(
            "coordinated release receipt operation differs"
        )
    if receipt.get("state") != "AUTHORIZED":
        raise ControlPanelValidationError("coordinated release receipt state differs")
    if receipt.get("coordinator") != "portfolio-release-builder":
        raise ControlPanelValidationError(
            "coordinated release receipt coordinator differs"
        )
    release_id = _receipt_text(receipt.get("release_id"), "release_id")
    _receipt_text(receipt.get("release_commit"), "release_commit")
    if _receipt_text(receipt.get("output_root"), "output_root") != str(
        output_root.resolve()
    ):
        raise ControlPanelValidationError(
            "coordinated release receipt output root differs"
        )
    for name, path in (("candidate", candidate_dir), ("qa", qa_dir)):
        identity = _object(receipt.get(name), f"coordinated release receipt {name}")
        if set(identity) != {"path", "file_count", "inventory_sha256"}:
            raise ControlPanelValidationError(
                f"coordinated release receipt {name} keys differ"
            )
        if _receipt_text(identity.get("path"), f"{name}.path") != str(path.resolve()):
            raise ControlPanelValidationError(
                f"coordinated release receipt {name} path differs"
            )
        if type(identity.get("file_count")) is not int or identity["file_count"] < 0:
            raise ControlPanelValidationError(
                f"coordinated release receipt {name} file count differs"
            )
        expected_hash = _receipt_text(
            identity.get("inventory_sha256"), f"{name}.inventory_sha256"
        )
        actual = _authorization_directory_identity(path, name)
        if (
            identity["file_count"] != actual["file_count"]
            or expected_hash != actual["inventory_sha256"]
        ):
            raise ControlPanelValidationError(
                f"coordinated release receipt {name} identity differs"
            )
    return receipt_sha256, release_id


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _receipt_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ControlPanelValidationError(
            f"coordinated release receipt {field_name} must be non-empty text"
        )
    return value


def _load_json(path: Path, field_name: str) -> dict[str, Any]:
    value, _content, _sha256 = _json_snapshot(path, field_name)
    return value


def _filesystem_path(path: Path) -> Path:
    """Use the native extended-length spelling for local Windows file I/O."""

    if os.name != "nt":
        return path
    # ``Path.absolute()`` preserves ``..`` segments.  Extended-length Windows
    # paths reject those segments, so collapse them lexically before adding the
    # prefix; unlike ``resolve()``, this does not follow symlinks and therefore
    # preserves the caller's link-rejection checks.
    value = os.path.normpath(str(path.absolute()))
    if len(value) < 248:
        return Path(value)
    if value.startswith("\\\\?\\"):
        return Path(value)
    if value.startswith("\\\\"):
        return Path(f"\\\\?\\UNC\\{value[2:]}")
    return Path(f"\\\\?\\{value}")


def _read_snapshot(path: Path, field_name: str) -> tuple[bytes, str]:
    """Read one regular-file snapshot and derive its identity from those bytes."""

    candidate = _filesystem_path(path)
    if candidate.is_symlink() or not candidate.is_file():
        raise ControlPanelValidationError(f"{field_name} is missing or linked")
    try:
        content = candidate.read_bytes()
    except OSError as exc:
        raise ControlPanelValidationError(f"{field_name} is not readable") from exc
    return content, hashlib.sha256(content).hexdigest()


def _json_snapshot(path: Path, field_name: str) -> tuple[dict[str, Any], bytes, str]:
    content, sha256 = _read_snapshot(path, field_name)
    return _json_from_bytes(content, field_name), content, sha256


def _json_from_bytes(content: bytes, field_name: str) -> dict[str, Any]:
    try:
        decoded = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControlPanelValidationError(f"{field_name} is not readable JSON") from exc
    return _object(decoded, field_name)


def _template_snapshot(path: Path) -> tuple[bytes, str]:
    content, sha256 = _read_snapshot(path, f"template {path.name}")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ControlPanelValidationError(
            f"template is not readable UTF-8: {path.name}"
        ) from exc
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8"), sha256


def _frontend_styles_source(
    template_root: Path, legacy: tuple[Path, bytes, str]
) -> tuple[Path, bytes, str]:
    """The stylesheet the panel ships: the in-repo one when it exists, else the legacy input.

    ``styles.css`` has no source in the repository; the builder has copied it from the
    legacy panel input.  A ``styles.css`` committed beside the other templates is read
    like them (normalised to LF, bound by its raw digest) and wins; without one the
    legacy bytes are used unchanged, so a checkout that has not committed it still builds.
    """

    in_repo = template_root / "styles.css"
    if not in_repo.is_file():
        return legacy
    content, raw_sha256 = _template_snapshot(in_repo)
    return in_repo, content, raw_sha256


def _validated_strategy_evidence(
    evidence: Mapping[str, Any],
    strategy_ids: set[str],
) -> dict[str, dict[str, Any]]:
    """Validate provider/account evidence without granting it override authority."""

    if evidence.get("schema_version") != 1:
        raise ControlPanelValidationError("strategy evidence schema is unsupported")
    as_of_date = _text(evidence.get("as_of_date"), "strategy evidence as_of_date")
    try:
        date.fromisoformat(as_of_date)
    except ValueError as exc:
        raise ControlPanelValidationError(
            "strategy evidence as_of_date is invalid"
        ) from exc
    rows = _objects(evidence.get("strategies"), "strategy evidence strategies")
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        strategy_id = _text(row.get("strategy_id"), "strategy evidence strategy_id")
        if strategy_id in by_id:
            raise ControlPanelValidationError(
                "strategy evidence identity is duplicated"
            )
        if strategy_id not in strategy_ids:
            raise ControlPanelValidationError(
                f"strategy evidence is outside the current boundary: {strategy_id}"
            )
        provider_id = _text(row.get("provider_id"), "strategy evidence provider_id")
        generation_id = _text(
            row.get("generation_id"), "strategy evidence generation_id"
        )
        if re.fullmatch(r"[0-9a-f]{64}", generation_id) is None:
            raise ControlPanelValidationError(
                f"strategy evidence generation identity is invalid: {strategy_id}"
            )
        generation_as_of_date = _text(
            row.get("generation_as_of_date"),
            "strategy evidence generation_as_of_date",
        )
        try:
            date.fromisoformat(generation_as_of_date)
        except ValueError as exc:
            raise ControlPanelValidationError(
                f"strategy evidence generation date is invalid: {strategy_id}"
            ) from exc
        evidence_layer = _text(
            row.get("evidence_layer"), "strategy evidence evidence_layer"
        )
        paths = [
            (name, _object(row[name], f"strategy evidence {name}"))
            for name in ("account", "provider")
            if row.get(name) is not None
        ]
        if not paths:
            raise ControlPanelValidationError(
                f"strategy evidence lacks an observed path: {strategy_id}"
            )
        for name, observed in paths:
            for field in ("observed_start", "observed_end", "return_observations"):
                if observed.get(field) is None:
                    raise ControlPanelValidationError(
                        f"strategy evidence {name} lacks {field}: {strategy_id}"
                    )
            try:
                observed_start = date.fromisoformat(str(observed["observed_start"]))
                observed_end = date.fromisoformat(str(observed["observed_end"]))
            except ValueError as exc:
                raise ControlPanelValidationError(
                    f"strategy evidence {name} date is invalid: {strategy_id}"
                ) from exc
            if observed_end < observed_start:
                raise ControlPanelValidationError(
                    f"strategy evidence {name} interval is reversed: {strategy_id}"
                )
            if (
                not isinstance(observed["return_observations"], int)
                or observed["return_observations"] <= 0
            ):
                raise ControlPanelValidationError(
                    f"strategy evidence {name} return observations are invalid: {strategy_id}"
                )
        by_id[strategy_id] = {
            **row,
            "evidence_layer": evidence_layer,
            "generation_id": generation_id,
            "generation_as_of_date": generation_as_of_date,
            "provider_id": provider_id,
        }
    return by_id


def _strategy_evidence(
    report: Mapping[str, Any],
    report_identity: Mapping[str, str],
    path: Path | None,
    strategy_ids: set[str],
    snapshot_bytes: bytes | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
    """Use report evidence as authority; an optional legacy file may only agree."""

    report_rows = _objects(
        report.get("strategy_provider_evidence", []),
        "strategy_provider_evidence",
    )
    if report_rows:
        payload = {
            "as_of_date": _text(report.get("as_of_date"), "report.as_of_date"),
            "schema_version": 1,
            "strategies": report_rows,
        }
        by_id = _validated_strategy_evidence(payload, strategy_ids)
        evidence_bytes = canonical_json_bytes(payload)
        if path is not None:
            external_content = (
                snapshot_bytes
                if snapshot_bytes is not None
                else _read_snapshot(path, "strategy evidence")[0]
            )
            external = _json_from_bytes(external_content, "strategy evidence")
            _validated_strategy_evidence(external, strategy_ids)
            if canonical_json_bytes(external) != evidence_bytes:
                raise ControlPanelValidationError(
                    "strategy evidence file contradicts canonical report"
                )
        return by_id, {
            "as_of_date": payload["as_of_date"],
            "path": path,
            "sha256": hashlib.sha256(evidence_bytes).hexdigest(),
            "size_bytes": len(evidence_bytes),
            "source": "CANONICAL_PORTFOLIO_REPORT",
            "report_sha256": report_identity["sha256"],
        }

    if path is None:
        return {}, None
    legacy_content = (
        snapshot_bytes
        if snapshot_bytes is not None
        else _read_snapshot(path, "strategy evidence")[0]
    )
    legacy = _json_from_bytes(legacy_content, "strategy evidence")
    by_id = _validated_strategy_evidence(legacy, strategy_ids)
    sha256 = hashlib.sha256(legacy_content).hexdigest()
    return by_id, {
        "as_of_date": _text(legacy.get("as_of_date"), "strategy evidence as_of_date"),
        "path": path,
        "sha256": sha256,
        "size_bytes": len(legacy_content),
        "source": "LEGACY_COMPATIBILITY_INPUT",
        "report_sha256": report_identity["sha256"],
    }


def _freshness_health(
    path: Path | None,
    portfolio_as_of: str,
    snapshot_bytes: bytes | None = None,
) -> list[dict[str, Any]]:
    """Render a governed freshness bundle into existing data-health rows."""

    if path is None:
        raise ControlPanelValidationError(
            "freshness health is mandatory for current portfolio claims"
        )
    payload = (
        _json_from_bytes(snapshot_bytes, "freshness health")
        if snapshot_bytes is not None
        else _load_json(path, "freshness health")
    )
    schema_version = payload.get("assessment_schema_version")
    expected_bundle_keys = {
        "assessment_schema_version",
        "as_of_date",
        "assessments",
        "policy" if schema_version == 2 else "policies",
    }
    if schema_version == 4:
        # Version 4 is dated at the data cutoff and carries the execution date.
        expected_bundle_keys.add("evaluated_on")
    with_ledger = frozenset(expected_bundle_keys | {"provider_ledger"})
    if schema_version == 4:
        # Version 4 is bound to the ledger it was judged against: the identity is
        # required, not optional, as in the formal schema and the pipeline gate.
        accepted_bundle_keys = {with_ledger}
    elif schema_version == 3:
        accepted_bundle_keys = {frozenset(expected_bundle_keys), with_ledger}
    else:
        accepted_bundle_keys = {frozenset(expected_bundle_keys)}
    if frozenset(payload) not in accepted_bundle_keys:
        raise ControlPanelValidationError("freshness health keys differ")
    if schema_version not in {2, 3, 4}:
        raise ControlPanelValidationError("freshness health schema is unsupported")
    if schema_version == 4:
        execution_date = date.fromisoformat(
            _text(payload.get("evaluated_on"), "freshness health evaluated_on")
        )
        data_date = date.fromisoformat(
            _text(payload.get("as_of_date"), "freshness health as_of_date")
        )
        if data_date != date.fromisoformat(portfolio_as_of):
            raise ControlPanelValidationError(
                "freshness health is not dated at the portfolio data cutoff"
            )
        if execution_date < data_date:
            raise ControlPanelValidationError(
                "freshness health was evaluated before its data date"
            )
    else:
        execution_date = None
    if "provider_ledger" in payload:
        provider_ledger = payload.get("provider_ledger")
        ledger_identity = _object(provider_ledger, "freshness provider ledger")
        if set(ledger_identity) != {
            "as_of_date",
            "catalog_sha256",
            "catalog_size_bytes",
            "generation_id",
            "manifest_sha256",
            "manifest_size_bytes",
        }:
            raise ControlPanelValidationError("freshness provider ledger keys differ")
        ledger_as_of = date.fromisoformat(
            _text(ledger_identity.get("as_of_date"), "freshness ledger as_of_date")
        )
        freshness_as_of = execution_date or date.fromisoformat(
            _text(payload.get("as_of_date"), "freshness health as_of_date")
        )
        if ledger_as_of > freshness_as_of:
            raise ControlPanelValidationError(
                "freshness provider ledger postdates the evaluation"
            )
        for field in ("catalog_sha256", "generation_id", "manifest_sha256"):
            if not re.fullmatch(
                r"[0-9a-f]{64}",
                _text(ledger_identity.get(field), f"freshness ledger {field}"),
            ):
                raise ControlPanelValidationError(
                    f"freshness provider ledger {field} differs"
                )
        for field in ("catalog_size_bytes", "manifest_size_bytes"):
            value = ledger_identity.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ControlPanelValidationError(
                    f"freshness provider ledger {field} differs"
                )
    as_of = _text(payload.get("as_of_date"), "freshness health as_of_date")
    if date.fromisoformat(as_of) < date.fromisoformat(portfolio_as_of):
        raise ControlPanelValidationError("freshness health predates the portfolio")
    if schema_version == 2:
        policy = FreshnessPolicy.from_dict(
            _object(payload.get("policy"), "freshness health policy")
        )
        policies = {policy.policy_id: policy}
    else:
        policies = {}
        for raw_policy in _objects(payload.get("policies"), "freshness policies"):
            policy = FreshnessPolicy.from_dict(raw_policy)
            if policy.policy_id in policies:
                raise ControlPanelValidationError(
                    "freshness policy identity is duplicated"
                )
            policies[policy.policy_id] = policy
        if not policies:
            raise ControlPanelValidationError("freshness policies are unavailable")
    rows = []
    accepted_states = {"FRESH", "LATE", "STALE", "FINITE_COMPLETE", "UNAVAILABLE"}
    expected = {
        "acquired_at",
        "blocks_current_consumers",
        "escalation_required",
        "evaluated_on",
        "fact_type",
        "last_accepted_date",
        "next_expected_date",
        "policy_id",
        "provider_id",
        "reason",
        "revision_state",
        "state",
        "subject_id",
    }
    if schema_version in {3, 4}:
        expected |= {
            "coverage_status",
            "lane",
            "required_for_current",
            "subject_type",
        }
    for item in _objects(payload.get("assessments"), "freshness assessments"):
        if set(item) != expected:
            raise ControlPanelValidationError("freshness assessment keys differ")
        state = _text(item.get("state"), "freshness state")
        if state not in accepted_states:
            raise ControlPanelValidationError("freshness state is unknown")
        if _text(item.get("evaluated_on"), "freshness evaluated_on") != as_of:
            raise ControlPanelValidationError("freshness evaluation date differs")
        for field in ("blocks_current_consumers", "escalation_required"):
            if not isinstance(item.get(field), bool):
                raise ControlPanelValidationError(f"freshness {field} must be boolean")
        revision_state = _text(item.get("revision_state"), "freshness revision_state")
        if revision_state not in {"ORIGINAL", "REVISED"}:
            raise ControlPanelValidationError("freshness revision state is unknown")
        acquired_at = item.get("acquired_at")
        if acquired_at is not None:
            acquired_at = _text(acquired_at, "freshness acquired_at")
            parsed_acquired = datetime.fromisoformat(acquired_at)
            if parsed_acquired.tzinfo is None or parsed_acquired.utcoffset() is None:
                raise ControlPanelValidationError(
                    "freshness acquired_at must be timezone-aware"
                )
        provider_id = _text(item.get("provider_id"), "freshness provider_id")
        subject_id = _text(item.get("subject_id"), "freshness subject_id")
        fact_type = _text(item.get("fact_type"), "freshness fact_type")
        policy_id = _text(item.get("policy_id"), "freshness policy_id")
        try:
            policy = policies[policy_id]
        except KeyError as exc:
            raise ControlPanelValidationError(
                "freshness assessment references an unknown policy"
            ) from exc
        lane = (
            "LEGACY"
            if schema_version == 2
            else _text(item.get("lane"), "freshness lane")
        )
        subject_type = (
            "STRATEGY"
            if schema_version == 2
            else _text(item.get("subject_type"), "freshness subject_type")
        )
        required_for_current = (
            True if schema_version == 2 else item.get("required_for_current")
        )
        if not isinstance(required_for_current, bool):
            raise ControlPanelValidationError(
                "freshness required_for_current must be boolean"
            )
        coverage_status = (
            "LEGACY_UNSPECIFIED"
            if schema_version == 2
            else _text(item.get("coverage_status"), "freshness coverage_status")
        )
        last = item.get("last_accepted_date")
        next_expected = item.get("next_expected_date")
        if last is not None:
            last = _text(last, "freshness last_accepted_date")
            date.fromisoformat(last)
        if next_expected is not None:
            next_expected = _text(next_expected, "freshness next_expected_date")
            date.fromisoformat(next_expected)
        reason = _text(item.get("reason"), "freshness reason")
        expected_assessment = policy.assess(
            None if last is None else date.fromisoformat(last),
            date.fromisoformat(as_of),
        ).to_dict()
        if schema_version in {3, 4} and not required_for_current:
            expected_assessment["blocks_current_consumers"] = False
            expected_assessment["escalation_required"] = False
        for field in (
            "blocks_current_consumers",
            "escalation_required",
            "evaluated_on",
            "next_expected_date",
            "policy_id",
            "reason",
            "state",
        ):
            if item.get(field) != expected_assessment[field]:
                raise ControlPanelValidationError(
                    f"freshness assessment is stale: {subject_id}"
                )
        rows.append(
            {
                "acquired_at": acquired_at,
                "blocks_current_consumers": item["blocks_current_consumers"],
                "category": "DATA_FRESHNESS",
                "escalation_required": item["escalation_required"],
                "evaluated_on": as_of,
                "fact_type": fact_type,
                "freshness_calendar": policy.calendar.value,
                "freshness_mode": policy.mode.value,
                "expected_interval_days": policy.expected_interval_days,
                "grace_days": policy.grace_days,
                "item": (
                    f"{provider_id}:{subject_id}:{fact_type}"
                    if schema_version == 2
                    else f"{provider_id}:{subject_id}:{lane}:{fact_type}"
                ),
                "known_gap": (
                    f"next expected {next_expected or 'not recurring'}; {reason}"
                ),
                "last_accepted_date": last,
                "next_expected_date": next_expected,
                "policy_id": policy_id,
                "provider_id": provider_id,
                "reconciled_on": as_of,
                "reason": reason,
                "revision_state": revision_state,
                "status": state,
                "stale_after_days": policy.stale_after_days,
                "subject_id": subject_id,
                "value": last,
                **(
                    {
                        "coverage_status": coverage_status,
                        "lane": lane,
                        "required_for_current": required_for_current,
                        "subject_type": subject_type,
                    }
                    if schema_version in {3, 4}
                    else {}
                ),
            }
        )
    return sorted(rows, key=lambda item: str(item["item"]))


def _hash(path: Path) -> str:
    return hash_file(path)[0]


def _template_bytes(path: Path) -> bytes:
    return _template_snapshot(path)[0]


def _current_report(root: Path) -> tuple[dict[str, Any], dict[str, str], Path]:
    (
        pointer,
        manifest,
        pointer_path,
        pointer_sha256,
        manifest_path,
        manifest_sha256,
        generation_snapshots,
    ) = _current_generation_snapshot(root, "portfolio report")
    artifact = next(
        (
            item
            for item in manifest.artifacts
            if item.artifact_id == "normalized/portfolio-report-data.json"
        ),
        None,
    )
    if artifact is None:
        raise ControlPanelValidationError(
            "report data artifact is missing from the generation manifest"
        )
    path = root / "generations" / pointer.generation_id / "portfolio-report-data.json"
    # The report bytes are read exactly once: hashed, compared against the
    # verified generation manifest's artifact hash AND size, and only then
    # decoded — no swap between verification and parsing can supply an
    # ungoverned baseline reference.
    try:
        content, report_sha256 = generation_snapshots["portfolio-report-data.json"]
    except KeyError as exc:
        raise ControlPanelValidationError("report data is missing") from exc
    if (report_sha256, len(content)) != (
        artifact.sha256,
        artifact.size_bytes,
    ):
        raise ControlPanelValidationError(
            "report data is not hash-bound by its generation manifest"
        )
    try:
        report = _object(json.loads(content.decode("utf-8")), "portfolio report data")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControlPanelValidationError(
            "portfolio report data is not readable JSON"
        ) from exc
    if isinstance(report.get("report_schema_version"), bool) or report.get(
        "report_schema_version"
    ) not in {1, 2, 3, 4, 5}:
        raise ControlPanelValidationError("portfolio report schema is unsupported")
    return (
        report,
        {
            "generation_id": pointer.generation_id,
            "manifest_path": str(manifest_path),
            "manifest_sha256": manifest_sha256,
            "pointer_path": str(pointer_path),
            "pointer_sha256": pointer_sha256,
            "sha256": artifact.sha256,
        },
        path,
    )


def _current_panel_qa(root: Path, *, required: tuple[str, ...]) -> dict[str, str]:
    """Bind the published panel's retained QA evidence to its own manifest.

    The panel producer stages with the bare `stage_generation` and then writes
    QA under `qa/<name>` while declaring the artifact id `evidence/<name>`, so
    the detached-bundle verifier cannot read it: the sidecars are absent and the
    declared ids are logical rather than on-disk paths.

    Each required name is resolved through that mapping and its bytes are
    compared to the manifest's recorded sha256 **and** size. Asserting only that
    an artifact id appears in the manifest would accept a claim with no bytes
    behind it, which is the property the retained-QA gate exists to deny.
    """

    (
        pointer,
        manifest,
        pointer_path,
        pointer_sha256,
        manifest_path,
        manifest_sha256,
        generation_snapshots,
    ) = _current_generation_snapshot(root, "portfolio panel")
    artifacts = {item.artifact_id: item for item in manifest.artifacts}
    for name in required:
        artifact = artifacts.get(f"evidence/{name}")
        if artifact is None:
            raise ControlPanelValidationError(
                f"panel retained QA evidence is missing from the manifest: {name}"
            )
        try:
            content, sha256 = generation_snapshots[f"qa/{name}"]
        except KeyError as exc:
            raise ControlPanelValidationError(
                f"panel retained QA evidence is missing: {name}"
            ) from exc
        if (sha256, len(content)) != (artifact.sha256, artifact.size_bytes):
            raise ControlPanelValidationError(
                f"panel retained QA evidence is not hash-bound by its manifest: {name}"
            )
    return {
        "generation_id": pointer.generation_id,
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha256,
        "pointer_path": str(pointer_path),
        "pointer_sha256": pointer_sha256,
    }


def _report_candidate(
    candidate_dir: Path,
) -> tuple[dict[str, Any], dict[str, str | None], Path]:
    """Bind one hash-addressed, non-published report candidate exactly once."""

    candidate = _filesystem_path(candidate_dir)
    if candidate.is_symlink() or not candidate.is_dir():
        raise ControlPanelValidationError(
            "report candidate directory is missing or linked"
        )
    report_path = candidate / "portfolio-report-data.json"
    content, sha256 = _read_snapshot(report_path, "report candidate data")
    if candidate.name != sha256:
        raise ControlPanelValidationError("report candidate directory identity differs")
    report = _json_from_bytes(content, "report candidate data")
    if isinstance(report.get("report_schema_version"), bool) or report.get(
        "report_schema_version"
    ) not in {1, 2, 3, 4, 5}:
        raise ControlPanelValidationError("report candidate schema is unsupported")
    return (
        report,
        {
            "generation_id": candidate.name,
            "manifest_path": None,
            "manifest_sha256": None,
            "pointer_path": None,
            "pointer_sha256": None,
            "sha256": sha256,
        },
        report_path,
    )


def _report_downloads(
    report_path: Path,
    report_identity: Mapping[str, str | None],
    report_root: Path | None,
    *,
    require_workbook: bool,
) -> tuple[dict[str, bytes], dict[str, tuple[Path, str]]]:
    """Deliver the selected report and its existing, report-bound workbook."""
    report_bytes, report_sha256 = _read_snapshot(report_path, "report download")
    if report_sha256 != report_identity["sha256"]:
        raise ControlPanelValidationError("report download changed after selection")
    files = {REPORT_DOWNLOAD: report_bytes}
    bindings: dict[str, tuple[Path, str]] = {}
    if not require_workbook:
        return files, bindings
    workbook_dir = (
        report_root / "deliveries" / str(report_identity["generation_id"])
        if report_root is not None
        else report_path.parent
    )
    workbook_path = workbook_dir / "portfolio-report.xlsx"
    if report_root is None and (workbook_dir / "excel" / workbook_path.name).exists():
        workbook_path = workbook_dir / "excel" / workbook_path.name
    if not workbook_path.exists() and not require_workbook:
        return files, bindings
    report_producer = import_module("portfolio_report")
    workbook_bytes, workbook_sha256 = _read_snapshot(workbook_path, "workbook download")
    if report_root is not None:
        delivery_path = workbook_dir / report_producer.REPORT_DELIVERY_NAME
        receipt_bytes, receipt_sha256 = _read_snapshot(delivery_path, "report delivery")
        delivery = report_producer.inspect_report_delivery(
            report_root, str(report_identity["generation_id"]), workbook_dir
        )
        if (
            delivery["workbook_status"] != "EXACT_NON_AUTHORITY"
            or delivery["actual_workbook_sha256"] != workbook_sha256
            or delivery["report_data_sha256"] != report_sha256
            or _read_snapshot(delivery_path, "report delivery")[0] != receipt_bytes
        ):
            raise ControlPanelValidationError(
                "workbook delivery differs from selected report"
            )
        bindings["report_delivery"] = (delivery_path, receipt_sha256)
    report_producer._workbook_parity_receipt(report_bytes, workbook_bytes)
    files[WORKBOOK_DOWNLOAD] = workbook_bytes
    bindings["report_workbook"] = (workbook_path, workbook_sha256)
    return files, bindings


def embedded_download_bytes(html: str, relative_path: str) -> bytes:
    """Recover a download the page carries inline rather than as a file.

    The workbook is delivered only as base64 payload elements inside
    `index.html`, because an .xlsx written into a published generation is
    rewritten in place by the SharePoint sync client and a sealed directory
    cannot tolerate that. Anything that needs those bytes -- the browser, and
    the financial-outcome validator's workbook parity receipt -- reads them from
    here, so all of them see the same bytes by construction.
    """

    metadata = _DOWNLOAD_METADATA.get(relative_path)
    if metadata is None:
        raise ControlPanelValidationError("embedded download is not a known payload")
    prefix = metadata[2]
    pattern = re.compile(
        r'<script type="application/octet-stream" id="'
        + re.escape(prefix)
        + r'-(\d{5})"([^>]*)>([^<]*)</script>'
    )
    found = pattern.findall(html)
    chunks = sorted(
        ((int(index), payload) for index, _attributes, payload in found),
        key=lambda item: item[0],
    )
    if not chunks or [index for index, _ in chunks] != list(range(len(chunks))):
        raise ControlPanelValidationError("embedded download payload is incomplete")
    encodings = {
        match.group(1)
        if (match := _ENCODING_ATTRIBUTE.search(attributes))
        else "identity"
        for _index, attributes, _payload in found
    }
    # A payload without the attribute predates #540 and is the file's own bytes.
    if len(encodings) != 1 or not encodings <= {"identity", "gzip"}:
        raise ControlPanelValidationError("embedded download encoding differs")
    try:
        encoded = base64.b64decode(
            "".join(payload for _, payload in chunks), validate=True
        )
    except (ValueError, binascii.Error) as exc:
        raise ControlPanelValidationError(
            "embedded download payload is not valid base64"
        ) from exc
    if encodings == {"gzip"}:
        try:
            return _gunzip_exact(encoded)
        except zlib.error as exc:
            raise ControlPanelValidationError(
                "embedded download payload is not valid gzip"
            ) from exc
    return encoded


def embedded_download_bytes_if_present(html: str, relative_path: str) -> bytes | None:
    """`embedded_download_bytes`, or None when the page carries no such payload.

    Only an absent payload is None; a payload that is present and unreadable raises.
    """

    metadata = _DOWNLOAD_METADATA.get(relative_path)
    if metadata is None:
        raise ControlPanelValidationError("embedded download is not a known payload")
    if f'data-download-payload-prefix="{metadata[2]}"' not in html:
        return None
    return embedded_download_bytes(html, relative_path)


_ENCODING_ATTRIBUTE: Final = re.compile(r'data-download-encoding="([a-z]+)"')


def _gunzip_exact(content: bytes) -> bytes:
    """Inflate one complete gzip member; trailing bytes are an error."""
    inflater = zlib.decompressobj(wbits=zlib.MAX_WBITS | 16)
    raw = inflater.decompress(content) + inflater.flush()
    if not inflater.eof or inflater.unused_data:
        raise zlib.error("gzip member is truncated or has trailing bytes")
    return raw


_PANEL_LENS_REPRESENTATION: Final = "SURFACE_SERIES_ONCE_V1"


def _panel_wire_form(data: Mapping[str, Any]) -> dict[str, Any]:
    """The published panel payload: every lens surface's series is carried once.

    The assembled payload repeats data two ways: each lens record repeats the
    series of its default surface (`lens.series` == `lens.surfaces[default].series`),
    and `decomposition_lenses.lenses` repeats `modes.LIVE_ONLY`. Together that is
    ~99 MB of the 364 MB payload. The repeats are removed only where they are
    exactly equal to the copy that stays, so no value is lost, and the schema
    names the representation.
    """

    lenses = data.get("decomposition_lenses")
    modes = lenses.get("modes") if isinstance(lenses, Mapping) else None
    if not isinstance(lenses, Mapping) or not isinstance(modes, Mapping):
        return dict(data)

    def deduplicated(row: Any) -> Any:
        if not isinstance(row, Mapping) or "series" not in row:
            return row
        surfaces = row.get("surfaces")
        default = row.get("default_surface")
        if (
            isinstance(surfaces, Mapping)
            and isinstance(surfaces.get(default), Mapping)
            and surfaces[default].get("series") == row["series"]
        ):
            return {key: value for key, value in row.items() if key != "series"}
        return row

    wire_modes = {
        mode: [deduplicated(row) for row in rows] if isinstance(rows, list) else rows
        for mode, rows in modes.items()
    }
    wire_lenses = {**lenses, "modes": wire_modes}
    if wire_lenses.get("lenses") == modes.get("LIVE_ONLY"):
        del wire_lenses["lenses"]
    wire_lenses["representation"] = _PANEL_LENS_REPRESENTATION
    return {**data, "decomposition_lenses": wire_lenses}


def _panel_data_parts(
    data: Mapping[str, Any], data_bytes: bytes
) -> list[tuple[str, bool, bytes]]:
    """Split the canonical panel JSON into ordered byte-exact (id, lazy, bytes) parts.

    The lazy part is the LIVE_PLUS_BACKTEST lens array; head + lazy + tail is the
    original byte string, which is asserted, so every reader of the parts sees the
    bytes that `data/dashboard_data.json` carries.
    """

    node: Any = data
    for key in _PANEL_LAZY_POINTER:
        node = node.get(key) if isinstance(node, Mapping) else None
    if not isinstance(node, list) or not node:
        return [("all", False, data_bytes)]
    # canonical_json_bytes ends with one newline, which is not part of the nested array.
    lazy_bytes = canonical_json_bytes(node)[:-1]
    start = data_bytes.find(lazy_bytes)
    if start < 0 or data_bytes.find(lazy_bytes, start + 1) >= 0:
        raise ControlPanelValidationError(
            "lazy lens array is not exactly one slice of the panel payload"
        )
    head = data_bytes[:start]
    tail = data_bytes[start + len(lazy_bytes) :]
    if head + lazy_bytes + tail != data_bytes:
        raise ControlPanelValidationError("panel payload parts differ from the payload")
    return [("head", False, head), ("lazy", True, lazy_bytes), ("tail", False, tail)]


def _panel_data_script(data: Mapping[str, Any], data_bytes: bytes) -> bytes:
    """`data/dashboard_data.js`: the panel payload as gzip parts, not one 364 MB literal."""

    parts = _panel_data_parts(data, data_bytes)
    # The renderer joins head + "[]" + tail into one string before JSON.parse, so
    # the eager parts are bounded together, not only one by one.
    eager_bytes = sum(len(raw) for _part_id, lazy, raw in parts if not lazy)
    if eager_bytes > _PANEL_PART_MAX_RAW_BYTES:
        raise ControlPanelValidationError(
            "panel data eager parts exceed the renderer string limit: "
            f"{eager_bytes} bytes > {_PANEL_PART_MAX_RAW_BYTES}"
        )
    described = []
    for part_id, lazy, raw in parts:
        if len(raw) > _PANEL_PART_MAX_RAW_BYTES:
            raise ControlPanelValidationError(
                f"panel data part {part_id} exceeds the renderer string limit: "
                f"{len(raw)} bytes > {_PANEL_PART_MAX_RAW_BYTES}"
            )
        described.append(
            {
                "id": part_id,
                "lazy": lazy,
                "raw_bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "gzip_base64": base64.b64encode(
                    gzip.compress(raw, compresslevel=_DOWNLOAD_GZIP_LEVEL, mtime=0)
                ).decode("ascii"),
            }
        )
    descriptor = {
        "encoding": "gzip",
        "lazy_pointer": list(_PANEL_LAZY_POINTER),
        "parts": described,
        "total_bytes": len(data_bytes),
        "total_sha256": hashlib.sha256(data_bytes).hexdigest(),
        "version": 1,
    }
    script = (
        f"window.{_PANEL_SCRIPT_GLOBAL} = ".encode("ascii")
        + json.dumps(descriptor, separators=(",", ":"), sort_keys=True).encode("ascii")
        + b";\n"
    )
    if len(script) > _PANEL_SCRIPT_MAX_BYTES:
        raise ControlPanelValidationError(
            "dashboard_data.js exceeds the panel size budget: "
            f"{len(script)} bytes > {_PANEL_SCRIPT_MAX_BYTES}"
        )
    return script


def _report_download_link(relative_path: str, label: str, content: bytes) -> str:
    """Render one fixed local-file-safe download with its exact selected bytes."""
    metadata = _DOWNLOAD_METADATA.get(relative_path)
    if metadata is None or not isinstance(content, bytes) or not content:
        raise ControlPanelValidationError("report download link input differs")
    filename, mime_type, payload_prefix = metadata
    encoding = _DOWNLOAD_ENCODINGS[relative_path]
    carried = (
        gzip.compress(content, compresslevel=_DOWNLOAD_GZIP_LEVEL, mtime=0)
        if encoding == "gzip"
        else content
    )
    payload = base64.b64encode(carried).decode("ascii")
    chunks = [
        payload[index : index + _DOWNLOAD_BASE64_CHUNK_SIZE]
        for index in range(0, len(payload), _DOWNLOAD_BASE64_CHUNK_SIZE)
    ]
    if len(chunks) > _DOWNLOAD_MAX_BASE64_CHUNKS:
        # Refuse here rather than emit a panel whose own download handler will
        # reject its metadata. Nothing bounded this before, so the producer
        # could publish an inert link and the cost surfaced six stages later as
        # a bare interaction-QA timeout naming nothing.
        raise ControlPanelValidationError(
            "report download payload exceeds the embedded download chunk cap: "
            f"{relative_path} needs {len(chunks)} chunks, the renderer accepts "
            f"{_DOWNLOAD_MAX_BASE64_CHUNKS}"
        )
    link = (
        f'<a class="ghost-button" href="{relative_path}" download="{filename}" '
        f'data-download-filename="{filename}" data-download-mime="{mime_type}" '
        f'data-download-payload-prefix="{payload_prefix}" '
        f'data-download-encoding="{encoding}" '
        f'data-download-raw-bytes="{len(content)}" '
        f'data-download-chunk-count="{len(chunks)}">{html_escape(label)}</a>'
    )
    payload_elements = "".join(
        f'<script type="application/octet-stream" '
        f'id="{payload_prefix}-{index:05d}" data-download-filename="{filename}" '
        f'data-download-encoding="{encoding}" '
        f'data-download-chunk-index="{index}">{chunk}</script>'
        for index, chunk in enumerate(chunks)
    )
    return link + payload_elements


def _verified_strategy_history_contract(
    source_path: Path,
    report_contract: Mapping[str, Any],
    dependence_selected_inputs_path: Path | None = None,
) -> tuple[dict[str, Any], tuple[Path, str]]:
    """Verify the report-bound dependence source needed for HPR HWM parity."""

    source_content, source_sha256 = _read_snapshot(
        source_path, "strategy history contract"
    )
    if source_path.name != "dependence-source.json":
        raise ControlPanelValidationError("strategy history contract filename differs")
    report_identity = _object(report_contract, "report.strategy_history_contract")
    expected_sha256 = _text(
        report_identity.get("sha256"), "strategy history contract sha256"
    )
    expected_size = report_identity.get("size_bytes")
    if not isinstance(expected_size, int) or expected_size < 1:
        raise ControlPanelValidationError("strategy history contract size differs")
    if (source_sha256, len(source_content)) != (expected_sha256, expected_size):
        raise ControlPanelValidationError(
            "strategy history contract differs from the report"
        )
    source_receipt = _json_from_bytes(source_content, "strategy history contract")
    try:
        current_dependence = import_module("tools.portfolio_current_dependence")
    except ModuleNotFoundError as exc:  # direct `python tools/...` execution
        if exc.name != "tools":
            raise
        current_dependence = import_module("portfolio_current_dependence")
    try:
        verified = current_dependence.verify_packaged_source(
            source_path.parent,
            contract_path=FINANCIAL_MVP_CONTRACT,
            policy_path=DEPENDENCE_POLICY,
            selected_inputs_path=dependence_selected_inputs_path,
        )
    except current_dependence.CurrentDependenceError as exc:
        raise ControlPanelValidationError(
            f"strategy history contract is invalid: {exc}"
        ) from exc
    if (
        source_receipt.get("bundle_id")
        != _text(report_identity.get("bundle_id"), "strategy history bundle_id")
        or verified.get("complete_through")
        != _text(report_identity.get("as_of_date"), "strategy history as_of_date")
        or source_receipt.get("returns_sha256")
        != _text(
            report_identity.get("returns_sha256"), "strategy history returns_sha256"
        )
        or source_receipt.get("returns_size_bytes")
        != report_identity.get("returns_size_bytes")
    ):
        raise ControlPanelValidationError(
            "strategy history contract differs from the report"
        )
    return verified, (source_path, source_sha256)


def _baseline_catalog_snapshot_for_report(
    baseline_root: Path | None, report: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, tuple[Path, str]]]:
    """Resolve the governed baseline generation the report binds to.

    The panel's reconciliation authority for the HWM envelope, the
    rollforward crosswalk, and the perimeter is the baseline CATALOG — never
    the report's carried copies.  There is no CURRENT indirection here: the
    report's baseline reference names the exact generation, resolved through
    the core library's integrity machinery (``read_generation`` parses the
    content-addressed manifest with full strict-key and identity
    validation).  The catalog bytes are read exactly ONCE: the same bytes
    are hashed, compared against the manifest's artifact hash AND size and
    the report's accepted catalog hash, and only then decoded.  The
    generation must meet publication eligibility (VERIFIED and
    COMPLETE_WITHIN_DECLARED_BOUNDARY).  A missing root, an unresolvable or
    ineligible generation, or any hash drift fails closed.
    """
    if baseline_root is None:
        raise ControlPanelValidationError(
            "baseline root is required for governed HWM reconciliation"
        )
    reference = _object(report.get("baseline"), "report.baseline")
    generation_id = _text(reference.get("generation_id"), "baseline.generation_id")
    expected_sha256 = _text(reference.get("catalog_sha256"), "baseline.catalog_sha256")
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise ControlPanelValidationError("baseline.catalog_sha256 is invalid")
    if baseline_root.is_symlink():
        raise ControlPanelValidationError("governed baseline root is linked")
    manifest_path = baseline_root / "generations" / generation_id / "generation.json"
    try:
        manifest_content, manifest_sha256 = _read_snapshot(
            manifest_path, "governed baseline generation manifest"
        )
        manifest = GenerationManifest.from_json_bytes(manifest_content)
    except (ManifestValidationError, OSError, ValueError, RuntimeError) as exc:
        raise ControlPanelValidationError(
            "governed baseline generation is missing or failed integrity"
        ) from exc
    if manifest.generation_id != generation_id:
        raise ControlPanelValidationError(
            "governed baseline generation identity differs"
        )
    if manifest.verification is not VerificationStatus.VERIFIED:
        raise ControlPanelValidationError(
            "governed baseline generation is not verification-complete"
        )
    if manifest.coverage.status is not CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY:
        raise ControlPanelValidationError(
            "governed baseline generation coverage is not complete"
        )
    artifact = next(
        (
            item
            for item in manifest.artifacts
            if item.artifact_id == "normalized/portfolio-baseline.json"
        ),
        None,
    )
    if artifact is None:
        raise ControlPanelValidationError(
            "baseline catalog artifact is missing from the generation manifest"
        )
    catalog_path = (
        baseline_root / "generations" / generation_id / "portfolio-baseline.json"
    )
    # Read the catalog bytes exactly once: hash and decode the same bytes so
    # no swap is possible between verification and parsing.
    content, catalog_sha256 = _read_snapshot(catalog_path, "baseline catalog")
    if (catalog_sha256, len(content)) != (
        artifact.sha256,
        artifact.size_bytes,
    ):
        raise ControlPanelValidationError(
            "baseline catalog is not hash-bound by its generation manifest"
        )
    if artifact.sha256 != expected_sha256:
        raise ControlPanelValidationError(
            "baseline catalog differs from the report's accepted reference"
        )
    try:
        catalog = _object(json.loads(content.decode("utf-8")), "baseline catalog")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControlPanelValidationError(
            "baseline catalog is not readable JSON"
        ) from exc
    if catalog.get("baseline_schema_version") not in {1, 2}:
        raise ControlPanelValidationError("baseline schema version is unsupported")
    return catalog, {
        "catalog": (catalog_path, catalog_sha256),
        "manifest": (manifest_path, manifest_sha256),
    }


def _baseline_catalog_for_report(
    baseline_root: Path | None, report: Mapping[str, Any]
) -> dict[str, Any]:
    catalog, _bindings = _baseline_catalog_snapshot_for_report(baseline_root, report)
    return catalog


def _current_generation_snapshot(
    root: Path, field_name: str
) -> tuple[
    CurrentPointer,
    GenerationManifest,
    Path,
    str,
    Path,
    str,
    dict[str, tuple[bytes, str]],
]:
    """Pin CURRENT and generation.json to the exact bytes used for authority."""

    try:
        if root.is_symlink():
            raise ControlPanelValidationError(f"{field_name} root is linked")
        pointer_path = root / "CURRENT.json"
        pointer_bytes, pointer_sha256 = _read_snapshot(
            pointer_path, f"{field_name} pointer"
        )
        pointer = CurrentPointer.from_json_bytes(pointer_bytes)
        manifest_path = root / "generations" / pointer.generation_id / "generation.json"
        manifest_bytes, manifest_sha256 = _read_snapshot(
            manifest_path, f"{field_name} generation manifest"
        )
        if manifest_sha256 != pointer.manifest_sha256:
            raise ControlPanelValidationError(
                f"{field_name} generation manifest differs from CURRENT"
            )
        manifest = GenerationManifest.from_json_bytes(manifest_bytes)
    except (
        OSError,
        ValueError,
        RuntimeError,
        ManifestValidationError,
        PointerIntegrityError,
    ) as exc:
        if isinstance(exc, ControlPanelValidationError):
            raise
        raise ControlPanelValidationError(
            f"{field_name} current generation is not accepted"
        ) from exc
    if manifest.generation_id != pointer.generation_id:
        raise ControlPanelValidationError(
            f"{field_name} generation identity differs from CURRENT"
        )
    if (
        manifest.verification is not VerificationStatus.VERIFIED
        or manifest.coverage.status
        is not CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY
    ):
        raise ControlPanelValidationError(
            f"{field_name} generation is not publication eligible"
        )
    generation_dir = manifest_path.parent
    filesystem_generation_dir = _filesystem_path(generation_dir)
    for directory, label in (
        (filesystem_generation_dir.parent, "generations root"),
        (filesystem_generation_dir, "generation directory"),
    ):
        if directory.is_symlink() or not directory.is_dir():
            raise ControlPanelValidationError(f"{field_name} {label} is invalid")
    snapshots: dict[str, tuple[bytes, str]] = {
        "generation.json": (manifest_bytes, manifest_sha256)
    }
    inventory_rows: list[dict[str, object]] = []
    for directory_name, directory_names, file_names in os.walk(
        filesystem_generation_dir, followlinks=False
    ):
        directory = Path(directory_name)
        for name in directory_names:
            if (directory / name).is_symlink():
                raise ControlPanelValidationError(
                    f"{field_name} generation contains a linked directory"
                )
        for name in file_names:
            path = directory / name
            relative = path.relative_to(filesystem_generation_dir).as_posix()
            if relative in snapshots:
                content, sha256 = snapshots[relative]
            else:
                content, sha256 = _read_snapshot(
                    path, f"{field_name} generation artifact {relative}"
                )
                snapshots[relative] = (content, sha256)
            inventory_rows.append(
                {
                    "relative_path": relative,
                    "sha256": sha256,
                    "size_bytes": len(content),
                }
            )
    inventory_rows.sort(key=lambda item: str(item["relative_path"]))
    if pointer.artifact_inventory_sha256 is not None:
        inventory_sha256 = hashlib.sha256(
            canonical_json_bytes(
                {"files": inventory_rows, "inventory_schema_version": 1}
            )
        ).hexdigest()
        if inventory_sha256 != pointer.artifact_inventory_sha256:
            raise ControlPanelValidationError(
                f"{field_name} generation artifact inventory differs"
            )
    return (
        pointer,
        manifest,
        pointer_path,
        pointer_sha256,
        manifest_path,
        manifest_sha256,
        snapshots,
    )


def _current_analytics(
    root: Path,
    *,
    filename: str,
    field_name: str,
    expected_schema_version: int | Sequence[int],
) -> tuple[dict[str, Any], dict[str, str], Path]:
    (
        pointer,
        manifest,
        pointer_path,
        pointer_sha256,
        manifest_path,
        manifest_sha256,
        generation_snapshots,
    ) = _current_generation_snapshot(root, field_name)
    path = root / "generations" / pointer.generation_id / filename
    try:
        content, sha256 = generation_snapshots[filename]
    except KeyError as exc:
        raise ControlPanelValidationError(f"{field_name} artifact is missing") from exc
    value = _json_from_bytes(content, field_name)
    accepted_versions = (
        (expected_schema_version,)
        if isinstance(expected_schema_version, int)
        else tuple(expected_schema_version)
    )
    if value.get("analytics_schema_version") not in accepted_versions:
        raise ControlPanelValidationError(f"{field_name} schema is unsupported")
    output_artifact = next(
        (
            item
            for item in manifest.artifacts
            if item.artifact_id == f"normalized/{filename}"
        ),
        None,
    )
    if (
        filename == "correlation.json"
        and value.get("analytics_schema_version") == 2
        and manifest.adapter.name == "portfolio-correlation"
        and manifest.adapter.version == "1.6.0"
        and output_artifact is None
    ):
        raise ControlPanelValidationError(
            "portfolio correlation output is not manifest-bound"
        )
    if output_artifact is not None and (
        output_artifact.sha256 != sha256 or output_artifact.size_bytes != len(content)
    ):
        raise ControlPanelValidationError(f"{field_name} artifact identity differs")
    return (
        value,
        {
            "generation_id": pointer.generation_id,
            "manifest_path": str(manifest_path),
            "manifest_sha256": manifest_sha256,
            "pointer_path": str(pointer_path),
            "pointer_sha256": pointer_sha256,
            "sha256": sha256,
        },
        path,
    )


def _current_replication(
    root: Path,
) -> tuple[dict[str, Any], dict[str, str], Path]:
    (
        pointer,
        manifest,
        pointer_path,
        pointer_sha256,
        manifest_path,
        manifest_sha256,
        generation_snapshots,
    ) = _current_generation_snapshot(root, "dynamic replication")
    path = root / "generations" / pointer.generation_id / "replication.json"
    try:
        content, sha256 = generation_snapshots["replication.json"]
    except KeyError as exc:
        raise ControlPanelValidationError(
            "dynamic replication artifact is missing"
        ) from exc
    value = _json_from_bytes(content, "dynamic replication")
    if value.get("replication_schema_version") != 1:
        raise ControlPanelValidationError("dynamic replication schema is unsupported")
    artifact = next(
        (
            item
            for item in manifest.artifacts
            if item.artifact_id == "normalized/replication.json"
        ),
        None,
    )
    if artifact is not None and (
        artifact.sha256 != sha256 or artifact.size_bytes != len(content)
    ):
        raise ControlPanelValidationError(
            "dynamic replication artifact identity differs"
        )
    return (
        value,
        {
            "generation_id": pointer.generation_id,
            "manifest_path": str(manifest_path),
            "manifest_sha256": manifest_sha256,
            "pointer_path": str(pointer_path),
            "pointer_sha256": pointer_sha256,
            "sha256": sha256,
        },
        path,
    )


def _candidate_driver_map_snapshot(
    path: Path | None,
) -> tuple[dict[str, Any] | None, dict[str, str] | None, bytes | None]:
    if path is None:
        return None, None, None
    value, content, sha256 = _json_snapshot(path, "candidate driver map")
    if value.get("schema_version") != 1:
        raise ControlPanelValidationError("candidate driver-map schema is unsupported")
    coverage = _object(value.get("coverage"), "candidate driver-map coverage")
    candidates = _objects(value.get("candidates"), "candidate driver-map candidates")
    clusters = _objects(value.get("driver_clusters"), "candidate driver-map clusters")
    leave_one_out = _objects(
        value.get("tactical_leave_one_out"), "candidate tactical leave-one-out"
    )
    authority = _object(
        value.get("authority_boundary"), "candidate driver-map authority"
    )
    expected_authority = {
        "allocation_effect": "NONE",
        "execution_effect": "NONE",
        "readiness_effect": "NONE",
        "automatic_add_drop_authority": "NONE",
    }
    if (
        coverage.get("candidate_count") != 33
        or len(candidates) != 33
        or len(clusters) != 5
        or len(leave_one_out) != 4
    ):
        raise ControlPanelValidationError("candidate driver-map cardinality differs")
    if authority != expected_authority:
        raise ControlPanelValidationError("candidate driver-map authority differs")
    as_of_date = _text(value.get("as_of_date"), "driver-map as_of_date")
    try:
        date.fromisoformat(as_of_date)
    except ValueError as exc:
        raise ControlPanelValidationError("driver-map as_of_date is invalid") from exc
    if value.get("research_status") != "FROZEN_EVIDENCE_NOT_CURRENT":
        raise ControlPanelValidationError(
            "candidate driver-map research status differs"
        )
    _validate_driver_map_coverage(coverage)
    _validate_driver_map_clusters(clusters)
    _validate_driver_map_leave_one_out(leave_one_out)
    _validate_driver_map_candidates(candidates)
    portfolio = _object(value.get("portfolio"), "candidate driver-map portfolio")
    _finite_float(
        portfolio.get("systematic_loss_limit"),
        "candidate driver-map systematic_loss_limit",
    )
    return (
        value,
        {
            "as_of_date": as_of_date,
            "sha256": sha256,
        },
        content,
    )


def _candidate_driver_map(
    path: Path | None,
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    value, identity, _content = _candidate_driver_map_snapshot(path)
    return value, identity


def _driver_map_integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ControlPanelValidationError(
            f"{field_name} must be a non-negative integer"
        )
    return value


def _driver_map_strings(value: object, field_name: str) -> list[str]:
    if not isinstance(value, list):
        raise ControlPanelValidationError(f"{field_name} must be an array")
    return [_text(item, field_name) for item in value]


def _driver_map_optional_number(value: object, field_name: str) -> None:
    if value is not None:
        _finite_float(value, field_name)


def _validate_driver_map_coverage(coverage: Mapping[str, Any]) -> None:
    measured = _driver_map_integer(
        coverage.get("measured_candidate_count"),
        "candidate driver-map measured_candidate_count",
    )
    unavailable = _driver_map_integer(
        coverage.get("unavailable_candidate_count"),
        "candidate driver-map unavailable_candidate_count",
    )
    if measured + unavailable != 33:
        raise ControlPanelValidationError("candidate driver-map coverage differs")
    _text(coverage.get("refresh_status"), "candidate driver-map refresh_status")


def _validate_driver_map_clusters(clusters: Sequence[Mapping[str, Any]]) -> None:
    cluster_ids = [
        _text(row.get("cluster_id"), "candidate driver-map cluster_id")
        for row in clusters
    ]
    if set(cluster_ids) != DRIVER_MAP_CLUSTER_IDS or len(cluster_ids) != len(
        set(cluster_ids)
    ):
        raise ControlPanelValidationError(
            "candidate driver-map cluster identity differs"
        )
    for row in clusters:
        cluster_id = _text(row.get("cluster_id"), "candidate driver-map cluster_id")
        _driver_map_strings(
            row.get("members"), f"candidate driver-map {cluster_id} members"
        )
        for field in ("allocation_weight", "adverse_loss_budget"):
            _finite_float(row.get(field), f"candidate driver-map {cluster_id} {field}")
        _text(
            row.get("budget_status"),
            f"candidate driver-map {cluster_id} budget_status",
        )
        observed_count = _driver_map_integer(
            row.get("observed_pair_count"),
            f"candidate driver-map {cluster_id} observed_pair_count",
        )
        for field in (
            "observed_pair_correlation_minimum",
            "observed_pair_correlation_maximum",
        ):
            value = row.get(field)
            if observed_count and value is None:
                raise ControlPanelValidationError(
                    f"candidate driver-map {cluster_id} {field} is missing"
                )
            _driver_map_optional_number(
                value, f"candidate driver-map {cluster_id} {field}"
            )


def _validate_driver_map_leave_one_out(
    rows: Sequence[Mapping[str, Any]],
) -> None:
    series_ids: set[str] = set()
    for row in rows:
        series_id = _text(
            row.get("series_id"), "candidate tactical leave-one-out series_id"
        )
        if series_id in series_ids:
            raise ControlPanelValidationError(
                "candidate tactical leave-one-out identity differs"
            )
        series_ids.add(series_id)
        for field in (
            "correlation_one_loss_reduction",
            "central_case_loss_reduction",
            "cluster_loss_without",
        ):
            _finite_float(row.get(field), f"candidate tactical leave-one-out {field}")
        _text(
            row.get("cluster_budget_status_without"),
            "candidate tactical leave-one-out cluster_budget_status_without",
        )


def _validate_driver_map_candidates(candidates: Sequence[Mapping[str, Any]]) -> None:
    candidate_ids: set[str] = set()
    for row in candidates:
        candidate_id = _text(
            row.get("candidate_id"), "candidate driver-map candidate_id"
        )
        if candidate_id in candidate_ids:
            raise ControlPanelValidationError("candidate driver-map identity differs")
        candidate_ids.add(candidate_id)
        for field in ("category", "tier", "portfolio_role"):
            _text(row.get(field), f"candidate driver-map {candidate_id} {field}")
        ticker = row.get("ticker")
        if ticker is not None:
            _text(ticker, f"candidate driver-map {candidate_id} ticker")
        cost = _object(row.get("cost"), f"candidate driver-map {candidate_id} cost")
        _text(cost.get("status"), f"candidate driver-map {candidate_id} cost status")
        for field in ("expense_ratio", "annual_fixed_cost"):
            _driver_map_optional_number(
                cost.get(field), f"candidate driver-map {candidate_id} cost {field}"
            )
        live_metrics = _object(
            row.get("live_metrics"),
            f"candidate driver-map {candidate_id} live_metrics",
        )
        live_status = _text(
            live_metrics.get("status"),
            f"candidate driver-map {candidate_id} live_metrics status",
        )
        if live_status.startswith("AVAILABLE"):
            for field in ("start_date", "end_date"):
                _text(
                    live_metrics.get(field),
                    f"candidate driver-map {candidate_id} live_metrics {field}",
                )
            _driver_map_integer(
                live_metrics.get("observations"),
                f"candidate driver-map {candidate_id} live_metrics observations",
            )
        else:
            _text(
                live_metrics.get("reason"),
                f"candidate driver-map {candidate_id} live_metrics reason",
            )
        scores = _objects(
            row.get("cluster_scores"),
            f"candidate driver-map {candidate_id} cluster_scores",
        )
        score_ids = [
            _text(
                score.get("cluster_id"),
                f"candidate driver-map {candidate_id} cluster score identity",
            )
            for score in scores
        ]
        if set(score_ids) != DRIVER_MAP_CLUSTER_IDS or len(score_ids) != len(
            set(score_ids)
        ):
            raise ControlPanelValidationError(
                f"candidate driver-map {candidate_id} cluster score identity differs"
            )
        tactical = next(
            score
            for score in scores
            if _text(
                score.get("cluster_id"),
                f"candidate driver-map {candidate_id} cluster score identity",
            )
            == "TACTICAL_TIMING_MULTI_ASSET"
        )
        _text(
            tactical.get("status"),
            f"candidate driver-map {candidate_id} tactical status",
        )
        _driver_map_strings(
            tactical.get("missing_members"),
            f"candidate driver-map {candidate_id} tactical missing_members",
        )
        downside = tactical.get("downside_correlation_mean")
        _driver_map_optional_number(
            downside,
            f"candidate driver-map {candidate_id} tactical downside_correlation_mean",
        )
        if downside is not None:
            for field in (
                "pearson_mean",
                "worst_decile_candidate_return_minimum",
            ):
                _finite_float(
                    tactical.get(field),
                    f"candidate driver-map {candidate_id} tactical {field}",
                )
            rank = _driver_map_integer(
                tactical.get("descriptive_rank"),
                f"candidate driver-map {candidate_id} tactical descriptive_rank",
            )
            if rank < 1:
                raise ControlPanelValidationError(
                    f"candidate driver-map {candidate_id} tactical descriptive_rank differs"
                )
        for field in ("window_start", "window_end"):
            window = tactical.get(field)
            if window is not None:
                _text(window, f"candidate driver-map {candidate_id} tactical {field}")


def _analytics_roots(
    report_root: Path | None,
    decomposition_root: Path | None,
    correlation_root: Path | None,
) -> tuple[Path, Path]:
    if report_root is None:
        if decomposition_root is None or correlation_root is None:
            raise ControlPanelValidationError(
                "report candidate requires explicit decomposition and correlation roots"
            )
        return decomposition_root, correlation_root
    analytics = report_root.parent / "analytics"
    return (
        decomposition_root or analytics / "decomposition",
        correlation_root or analytics / "correlation",
    )


def _source_inputs_snapshot(
    legacy_panel: Path,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, tuple[Path, bytes, str]],
]:
    contract, contract_content, contract_sha256 = _json_snapshot(
        SOURCE_CONTRACT, "control-panel source contract"
    )
    if contract.get("contract_version") != 1:
        raise ControlPanelValidationError(
            "control-panel source contract is unsupported"
        )
    source_files = _object(contract.get("source_files"), "source_files")
    observed: dict[str, str] = {}
    snapshots: dict[str, tuple[Path, bytes, str]] = {
        "source_contract": (SOURCE_CONTRACT, contract_content, contract_sha256)
    }
    raw_source_matches = True
    mismatched_relative: str | None = None
    for relative, expected in source_files.items():
        path = legacy_panel / relative
        try:
            content, actual = _read_snapshot(path, f"legacy source {relative}")
        except ControlPanelValidationError:
            raw_source_matches = False
            mismatched_relative = mismatched_relative or relative
            continue
        snapshots[f"source:{relative}"] = (path, content, actual)
        if actual != _text(expected, f"source_files.{relative}"):
            raw_source_matches = False
            mismatched_relative = mismatched_relative or relative
        observed[relative] = actual
    appendix = _object(contract.get("frozen_appendix"), "frozen_appendix")
    data_relative = _text(appendix.get("data_file"), "frozen_appendix.data_file")
    data_path = legacy_panel / data_relative
    data_content, data_sha256 = _read_snapshot(data_path, "legacy dashboard data")
    snapshots["legacy_data"] = (data_path, data_content, data_sha256)
    source_mode = "RAW_FROZEN_SOURCE"
    if raw_source_matches and data_sha256 == appendix.get("data_sha256"):
        legacy = _json_from_bytes(data_content, "frozen candidate appendix")
    else:
        rolled = _json_from_bytes(data_content, "rolled control-panel payload")
        if rolled.get("candidate_appendix") is None:
            suffix = f": {mismatched_relative}" if mismatched_relative else ""
            raise ControlPanelValidationError(f"legacy source hash differs{suffix}")
        candidate_path = legacy_panel / "candidate.json"
        candidate, candidate_content, candidate_sha256 = _json_snapshot(
            candidate_path, "rolled control-panel candidate"
        )
        snapshots["legacy_candidate"] = (
            candidate_path,
            candidate_content,
            candidate_sha256,
        )
        artifact_hashes = _object(
            candidate.get("artifact_sha256"), "rolled candidate artifact_sha256"
        )
        if artifact_hashes.get(data_relative) != data_sha256:
            raise ControlPanelValidationError(
                "rolled control-panel payload hash differs"
            )
        meta = _object(rolled.get("meta"), "rolled control-panel meta")
        observed = _object(
            meta.get("source_shell_hashes"), "rolled source_shell_hashes"
        )
        if observed != source_files:
            raise ControlPanelValidationError(
                "rolled frozen source-shell hashes differ"
            )
        legacy = _object(rolled.get("candidate_appendix"), "rolled candidate appendix")
        normalized_sha256 = _text(
            appendix.get("normalized_sha256"),
            "frozen_appendix.normalized_sha256",
        )
        if (
            hashlib.sha256(canonical_json_bytes(legacy)).hexdigest()
            != normalized_sha256
        ):
            raise ControlPanelValidationError(
                "rolled frozen appendix canonical hash differs"
            )
        if legacy.get("source_sha256") != appendix.get("data_sha256"):
            raise ControlPanelValidationError(
                "rolled frozen appendix source hash differs"
            )
        if legacy.get("as_of_date") != appendix.get("as_of_date") or legacy.get(
            "status"
        ) != appendix.get("status"):
            raise ControlPanelValidationError("rolled frozen appendix boundary differs")
        source_mode = "GOVERNED_PANEL_ROLLFORWARD"
    providers = _objects(legacy.get("providers"), "legacy.providers")
    if len(providers) != appendix.get("candidate_count"):
        raise ControlPanelValidationError("frozen candidate count differs")
    return (
        legacy,
        {
            "contract": contract,
            "observed_hashes": observed,
            "source_mode": source_mode,
        },
        snapshots,
    )


def _source_inputs(legacy_panel: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    legacy, source, _snapshots = _source_inputs_snapshot(legacy_panel)
    return legacy, source


def _dependence_strategy_names(
    strategy_names: Mapping[str, str],
    contract_snapshot: bytes | None = None,
) -> set[str]:
    contract = (
        _json_from_bytes(contract_snapshot, "financial MVP contract")
        if contract_snapshot is not None
        else _load_json(FINANCIAL_MVP_CONTRACT, "financial MVP contract")
    )
    product = _object(contract.get("product"), "financial MVP product")
    if product.get("product_id") != "FINANCIAL_MVP" or contract.get(
        "contract_schema_version"
    ) not in {6, 7}:
        raise ControlPanelValidationError("financial MVP contract identity differs")
    required_ids = {
        _text(row.get("strategy_id"), "financial MVP strategy_id")
        for row in _objects(contract.get("strategies"), "financial MVP strategies")
        if row.get("product_requirement") == "REQUIRED"
        and _object(row.get("outcome_dispositions"), "strategy outcomes").get(
            "DEPENDENCE"
        )
        == "REQUIRED"
    }
    if len(required_ids) != 7:
        raise ControlPanelValidationError(
            "financial MVP dependence strategy perimeter differs"
        )
    if not required_ids <= set(strategy_names):
        raise ControlPanelValidationError(
            "financial MVP dependence strategy identities are absent"
        )
    return {strategy_names[strategy_id] for strategy_id in required_ids}


def _financial_mvp_product_controls(
    contract_snapshot: bytes | None = None,
) -> dict[str, dict[str, Any]]:
    """Load the lifecycle authority that bounds current panel surfaces."""

    contract = (
        _json_from_bytes(contract_snapshot, "financial MVP contract")
        if contract_snapshot is not None
        else _load_json(FINANCIAL_MVP_CONTRACT, "financial MVP contract")
    )
    product = _object(contract.get("product"), "financial MVP product")
    if product.get("product_id") != "FINANCIAL_MVP" or contract.get(
        "contract_schema_version"
    ) not in {6, 7}:
        raise ControlPanelValidationError("financial MVP contract identity differs")
    controls: dict[str, dict[str, Any]] = {}
    strategy_rows = _objects(contract.get("strategies"), "financial MVP strategies")
    rows_by_id: dict[str, Mapping[str, Any]] = {}
    for row in strategy_rows:
        strategy_id = _text(row.get("strategy_id"), "financial MVP strategy_id")
        if strategy_id in controls:
            raise ControlPanelValidationError(
                "financial MVP strategy controls contain a duplicate"
            )
        controls[strategy_id] = {
            "lifecycle_status": _text(
                row.get("lifecycle_status"), "financial MVP lifecycle_status"
            ),
            "product_requirement": _text(
                row.get("product_requirement"), "financial MVP product_requirement"
            ),
        }
        funded_membership = row.get("funded_membership")
        if funded_membership is not None:
            membership = _object(
                funded_membership,
                f"financial MVP funded_membership {strategy_id}",
            )
            status = _text(
                membership.get("status"),
                f"financial MVP funded_membership status {strategy_id}",
            )
            if status not in {"EVIDENCED", "EXCLUDED"}:
                raise ControlPanelValidationError(
                    "financial MVP funded membership status is unsupported"
                )
            for field in ("effective_start", "effective_end"):
                value = membership.get(field)
                if value is not None:
                    try:
                        parsed = date.fromisoformat(
                            _text(
                                value,
                                f"financial MVP funded_membership {field} {strategy_id}",
                            )
                        )
                    except ValueError as exc:
                        raise ControlPanelValidationError(
                            "financial MVP funded membership interval is invalid"
                        ) from exc
                    if parsed.isoformat() != value:
                        raise ControlPanelValidationError(
                            "financial MVP funded membership interval is invalid"
                        )
            start = membership.get("effective_start")
            end = membership.get("effective_end")
            if status == "EVIDENCED" and not isinstance(start, str):
                raise ControlPanelValidationError(
                    "financial MVP evidenced membership requires effective_start"
                )
            if (
                start is not None
                and end is not None
                and date.fromisoformat(start) > date.fromisoformat(end)
            ):
                raise ControlPanelValidationError(
                    "financial MVP funded membership interval is reversed"
                )
            controls[strategy_id]["funded_membership"] = {
                "status": status,
                "effective_start": start,
                "effective_end": end,
            }
        rows_by_id[strategy_id] = row
    owner_perimeter_present = CURRENT_PRODUCT_STRATEGY_IDS <= set(controls)
    if owner_perimeter_present:
        # Nested history-only strategies (#567) join the pinned nine: each is an
        # excluded, deprecated record the nested-strategy tag map names.
        for strategy_id in set(controls) - CURRENT_PRODUCT_STRATEGY_IDS:
            if {
                key: controls[strategy_id].get(key)
                for key in ("lifecycle_status", "product_requirement")
            } != {
                "lifecycle_status": "DEPRECATED_HISTORY_ONLY",
                "product_requirement": "EXCLUDED",
            }:
                raise ControlPanelValidationError(
                    "nested history-only strategy control differs"
                )
    elif len(controls) != 9 and not (
        set(controls) & CURRENT_PRODUCT_STRATEGY_IDS
        or any("effective_product_interval" in row for row in strategy_rows)
    ):
        # An owner-bound contract that lost a pinned strategy is refused by the
        # identity check below, which names the real defect.
        raise ControlPanelValidationError(
            "financial MVP contract must contain 9 strategy controls"
        )
    if (
        sum(
            control["product_requirement"] == "REQUIRED"
            for control in controls.values()
        )
        != 7
    ):
        raise ControlPanelValidationError(
            "financial MVP contract must contain 7 required strategies"
        )
    # Production authority is identity-bound.  Keep the deliberately
    # synthetic unit-test contract compatible, but once any owner strategy is
    # present require the complete named perimeter; a seven-row count alone
    # is not an authority check.
    owner_bound_contract = bool(set(controls) & CURRENT_PRODUCT_STRATEGY_IDS) or any(
        "effective_product_interval" in row for row in strategy_rows
    )
    if owner_bound_contract:
        if not owner_perimeter_present:
            raise ControlPanelValidationError(
                "financial MVP strategy identity perimeter differs"
            )
    echo_control = controls.get("echotrade-growth-pivots")
    if echo_control is not None:
        echo_interval = _object(
            rows_by_id["echotrade-growth-pivots"].get("effective_product_interval"),
            "EchoTrade effective_product_interval",
        )
        if {
            key: echo_control.get(key)
            for key in ("lifecycle_status", "product_requirement")
        } != {
            "lifecycle_status": "DEPRECATED_HISTORY_ONLY",
            "product_requirement": "EXCLUDED",
        } or echo_interval.get("effective_end") != "2025-11-24":
            raise ControlPanelValidationError(
                "deprecated EchoTrade owner authority differs"
            )
    composer_control = controls.get("composer-holy-grail")
    if composer_control is not None and {
        key: composer_control.get(key)
        for key in ("lifecycle_status", "product_requirement")
    } != {
        "lifecycle_status": "IMMATERIAL_EXCLUDED",
        "product_requirement": "EXCLUDED",
    }:
        raise ControlPanelValidationError("Composer owner exclusion differs")
    if owner_perimeter_present and any(
        controls[strategy_id].get("lifecycle_status") != "ACTIVE"
        or controls[strategy_id].get("product_requirement") != "REQUIRED"
        for strategy_id in CURRENT_REQUIRED_STRATEGY_IDS
    ):
        raise ControlPanelValidationError(
            "financial MVP required strategy authority differs"
        )
    return controls


def _current_product_baseline_projection(
    catalog: Mapping[str, Any],
    strategy_product_controls: Mapping[str, Mapping[str, Any]],
    calculation_cutoff_date: date | None = None,
) -> dict[str, Any]:
    """Independently project current HWM authority from a retained baseline.

    A report cutoff limits only the two time-series collections compared back
    to the report.  The retained baseline can safely carry later observations.
    """

    required_ids = {
        strategy_id
        for strategy_id, control in strategy_product_controls.items()
        if control.get("product_requirement") == "REQUIRED"
    }
    if len(required_ids) != 7:
        raise ControlPanelValidationError(
            "current-product baseline projection requires 7 strategies"
        )

    def current_strategy_rows(value: Any, field_name: str) -> list[dict[str, Any]]:
        projected: list[dict[str, Any]] = []
        for row in _objects(value, field_name):
            strategy_id = row.get("strategy_id")
            if not isinstance(strategy_id, str):
                raise ControlPanelValidationError(
                    f"{field_name} strategy_id must be text"
                )
            control = strategy_product_controls.get(strategy_id)
            if control is None:
                raise ControlPanelValidationError(
                    f"{field_name} references an unknown product strategy"
                )
            if strategy_id not in required_ids:
                continue
            membership = control.get("funded_membership")
            if membership is not None and membership.get("status") != "EVIDENCED":
                continue
            if membership is None and calculation_cutoff_date is None:
                projected.append(dict(row))
                continue
            observation = row.get("date")
            if not isinstance(observation, str):
                raise ControlPanelValidationError(
                    f"{field_name} observation date must be text"
                )
            try:
                observation_date = date.fromisoformat(observation)
            except ValueError as exc:
                raise ControlPanelValidationError(
                    f"{field_name} observation date is invalid"
                ) from exc
            if (
                calculation_cutoff_date is not None
                and observation_date > calculation_cutoff_date
            ):
                continue
            if membership is None:
                projected.append(dict(row))
                continue
            try:
                start_date = date.fromisoformat(str(membership["effective_start"]))
                end_date = (
                    date.fromisoformat(str(membership["effective_end"]))
                    if membership.get("effective_end") is not None
                    else None
                )
            except ValueError as exc:
                raise ControlPanelValidationError(
                    f"{field_name} funded membership interval is invalid"
                ) from exc
            if observation_date < start_date or (
                end_date is not None and observation_date > end_date
            ):
                continue
            projected.append(dict(row))
        return projected

    allocations: list[dict[str, Any]] = []
    for row in _objects(catalog.get("allocations"), "allocations"):
        scope = row.get("scope")
        strategy_id = row.get("strategy_id")
        if scope == "RESERVE":
            if strategy_id is not None:
                raise ControlPanelValidationError(
                    "reserve allocation must not reference a strategy"
                )
            allocations.append(dict(row))
            continue
        if scope != "SYSTEMATIC" or not isinstance(strategy_id, str):
            raise ControlPanelValidationError(
                "current systematic allocation strategy_id must be text"
            )
        control = strategy_product_controls.get(strategy_id)
        if control is None:
            raise ControlPanelValidationError(
                "current systematic allocation references an unknown product strategy"
            )
        if strategy_id in required_ids:
            allocations.append(dict(row))

    projection = dict(catalog)
    projection["allocations"] = allocations
    projection["strategy_returns"] = current_strategy_rows(
        catalog.get("strategy_returns", []), "strategy_returns"
    )
    raw_hwm_inputs = catalog.get("hwm_inputs")
    if raw_hwm_inputs is not None:
        hwm_inputs = dict(_object(raw_hwm_inputs, "hwm_inputs"))
        hwm_inputs["periods"] = current_strategy_rows(
            hwm_inputs.get("periods"), "hwm_inputs.periods"
        )
        projection["hwm_inputs"] = hwm_inputs
    return projection


def _validate_current_product_baseline_projection(
    report: Mapping[str, Any],
    baseline_catalog: Mapping[str, Any],
    strategy_product_controls: Mapping[str, Mapping[str, str]],
) -> dict[str, Any]:
    """Bind report HWM inputs and returns to an independent current projection."""

    calculation_cutoff_date = (
        _panel_date(
            report["calculation_cutoff_date"],
            "report.calculation_cutoff_date",
        )
        if "calculation_cutoff_date" in report
        else None
    )
    projection = _current_product_baseline_projection(
        baseline_catalog, strategy_product_controls, calculation_cutoff_date
    )
    if report.get("hwm_inputs") != projection.get("hwm_inputs"):
        raise ControlPanelValidationError(
            "report hwm_inputs differs from the current-product baseline projection"
        )
    if report.get("strategy_returns", []) != projection.get("strategy_returns", []):
        raise ControlPanelValidationError(
            "report strategy_returns differ from the current-product baseline projection"
        )
    return projection


def _validate_current_product_boundary(
    report: Mapping[str, Any],
    strategy_product_controls: Mapping[str, Mapping[str, str]],
) -> None:
    """Reject excluded subjects that re-enter a current report or panel path."""

    excluded_ids = {
        strategy_id
        for strategy_id, control in strategy_product_controls.items()
        if control.get("product_requirement") == "EXCLUDED"
    }
    for row in _objects(report.get("strategy_summary"), "strategy_summary"):
        strategy_id = _text(row.get("strategy_id"), "strategy_summary.strategy_id")
        if strategy_id not in strategy_product_controls:
            raise ControlPanelValidationError(
                "strategy_summary references an unknown product strategy"
            )
        if strategy_id not in excluded_ids:
            continue
        authority = row.get("account_live_return_authority")
        if (
            row.get("account_live_status") == "AVAILABLE"
            or isinstance(authority, str)
            and authority.startswith("CANONICAL")
        ):
            raise ControlPanelValidationError(
                "excluded strategy carries current account-live authority"
            )
        raise ControlPanelValidationError(
            "excluded strategy appears on a current panel surface"
        )
    for row in _objects(report.get("twr_mwr"), "twr_mwr"):
        twr_strategy_id = row.get("strategy_id")
        if not isinstance(twr_strategy_id, str):
            raise ControlPanelValidationError("twr_mwr strategy_id must be text")
        if twr_strategy_id not in strategy_product_controls:
            raise ControlPanelValidationError(
                "twr_mwr references an unknown product strategy"
            )
        if twr_strategy_id not in excluded_ids:
            continue
        authority = row.get("return_path_authority")
        if (
            str(row.get("twr_status", "")).startswith("AVAILABLE")
            or isinstance(authority, str)
            and authority.startswith("CANONICAL")
        ):
            raise ControlPanelValidationError(
                "excluded strategy carries current account-live authority"
            )
        raise ControlPanelValidationError(
            "excluded strategy appears on a current panel surface"
        )
    for row in _objects(report.get("allocation_history"), "allocation_history"):
        if row.get("scope") != "SYSTEMATIC":
            continue
        allocation_strategy_id = row.get("strategy_id")
        if not isinstance(allocation_strategy_id, str):
            raise ControlPanelValidationError(
                "current systematic allocation strategy is not REQUIRED"
            )
        if (
            strategy_product_controls.get(allocation_strategy_id, {}).get(
                "product_requirement"
            )
            != "REQUIRED"
        ):
            raise ControlPanelValidationError(
                "current systematic allocation strategy is not REQUIRED"
            )


_RETAINED_RECORD_KIND: Final = "RETAINED_HISTORY_ONLY"


def _current_strategy_rows(
    data: Mapping[str, Any], label: str = "strategies"
) -> list[dict[str, Any]]:
    """The panel's current strategy rows, without the retained history-only records.

    Retained records (#592) are appended to ``strategies`` for the lifecycle view;
    they carry no capital, return or underwriting facts, so every current-product
    check reads only the rows that are not marked retained.
    """

    return [
        row
        for row in _objects(data.get("strategies"), label)
        if row.get("record_kind") != _RETAINED_RECORD_KIND
    ]


def _current_product_rows(
    rows: Sequence[Mapping[str, Any]],
    strategy_product_controls: Mapping[str, Mapping[str, str]],
) -> list[dict[str, Any]]:
    """Return only rows admitted to a current-product panel surface."""

    accepted: list[dict[str, Any]] = []
    for row in rows:
        strategy_id = row.get("strategy_id")
        if row.get("scope") == "RESERVE" and strategy_id is None:
            accepted.append(dict(row))
            continue
        if not isinstance(strategy_id, str):
            continue
        if (
            strategy_product_controls.get(strategy_id, {}).get("product_requirement")
            == "REQUIRED"
        ):
            accepted.append(dict(row))
    return accepted


def _baseline_correlation_strategy_mapping(
    baseline_catalog: Mapping[str, Any],
    current_strategy_names: Mapping[str, str],
) -> dict[str, str] | None:
    """Return an exact baseline-governed nine-strategy identity mapping."""

    raw_rows = baseline_catalog.get("strategies")
    if raw_rows is None:
        return None
    rows = _objects(raw_rows, "baseline strategies")
    mapping: dict[str, str] = {}
    for row in rows:
        strategy_id = _text(row.get("strategy_id"), "baseline strategy_id")
        strategy_name = _text(row.get("name"), "baseline strategy name")
        if strategy_id in mapping:
            raise ControlPanelValidationError(
                "baseline correlation strategy mapping contains a duplicate"
            )
        mapping[strategy_id] = strategy_name
    # Nested history-only strategies (#567) may follow the pinned nine; the
    # correlation mapping is the nine.
    if CURRENT_PRODUCT_STRATEGY_IDS <= set(mapping):
        mapping = {
            strategy_id: mapping[strategy_id]
            for strategy_id in sorted(CURRENT_PRODUCT_STRATEGY_IDS)
        }
    if (
        set(mapping) != set(CURRENT_PRODUCT_STRATEGY_IDS)
        or len(set(mapping.values())) != len(mapping)
        or set(current_strategy_names) != set(CURRENT_REQUIRED_STRATEGY_IDS)
        or any(
            mapping[strategy_id] != current_strategy_names[strategy_id]
            for strategy_id in CURRENT_REQUIRED_STRATEGY_IDS
        )
    ):
        raise ControlPanelValidationError(
            "baseline correlation strategy mapping identity differs"
        )
    return mapping


def _current_account_rows(
    rows: Sequence[Mapping[str, Any]], current_account_ids: set[str]
) -> list[dict[str, Any]]:
    """Keep only current-account facts while preserving accountless sentinels."""

    return [
        dict(row)
        for row in rows
        if isinstance(row.get("account_id"), str)
        and row["account_id"] in current_account_ids
    ]


def _referenced_current_account_ids(
    rows: Sequence[Mapping[str, Any]],
) -> set[str]:
    """Collect account references while preserving the explicit null sentinel."""

    result: set[str] = set()
    for row in rows:
        account_id = row.get("account_id")
        if not isinstance(account_id, str):
            raise ControlPanelValidationError("account reference must be text")
        result.add(_text(account_id, "account reference"))
    return result


def _derived_epoch_parent(
    account_id: str, parent_account_ids: Collection[str]
) -> str | None:
    """The one baseline account a derived sleeve account ``<parent>-<sleeve>`` hangs on."""

    parents = [
        parent for parent in parent_account_ids if account_id.startswith(f"{parent}-")
    ]
    return parents[0] if len(parents) == 1 else None


def _unresolved_account_references(
    account_ids: Collection[str],
    reference_ids: Collection[str],
    manifest_rows: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Account references the privacy-safe account dimension does not resolve.

    A reference resolves to a listed account, or to a derived sleeve account (#567): a
    manifest row that names it and hangs on exactly one listed account.  Nothing else
    resolves; a name that merely extends a listed account does not.
    """

    listed = set(account_ids)
    derived = {
        str(row["account_id"])
        for row in manifest_rows
        if isinstance(row.get("account_id"), str)
        and row["account_id"] not in listed
        and _derived_epoch_parent(str(row["account_id"]), listed) is not None
    }
    return sorted(set(reference_ids) - listed - derived)


def _history_only_strategy_ids(
    strategy_product_controls: Mapping[str, Mapping[str, str]],
) -> frozenset[str]:
    """Nested history-only strategies beyond the pinned perimeter (#567)."""

    if not CURRENT_PRODUCT_STRATEGY_IDS <= set(strategy_product_controls):
        return frozenset()
    return frozenset(
        strategy_id
        for strategy_id, control in strategy_product_controls.items()
        if strategy_id not in CURRENT_PRODUCT_STRATEGY_IDS
        and control.get("product_requirement") == "EXCLUDED"
        and control.get("lifecycle_status") == "DEPRECATED_HISTORY_ONLY"
    )


def _plan_without_retained_records(
    plan_strategies: Mapping[str, str], contract_snapshot: bytes | None
) -> dict[str, str]:
    """A decomposition plan's strategies without the contract's retained records (#567).

    The plan carries the history-only records beside the product strategies.  Every
    panel and report check of the plan against the product perimeter reads it through
    this one rule: the retained ids are the contract's EXCLUDED and
    DEPRECATED_HISTORY_ONLY strategies, nothing inferred from the plan itself.
    """

    retained = _history_only_strategy_ids(
        _financial_mvp_product_controls(
            contract_snapshot
            if contract_snapshot is not None
            else FINANCIAL_MVP_CONTRACT.read_bytes()
        )
    )
    return {
        strategy_id: name
        for strategy_id, name in plan_strategies.items()
        if strategy_id not in retained
    }


def _validate_current_product_account_manifest(
    account_manifest: Mapping[str, Any],
    strategy_product_controls: Mapping[str, Mapping[str, str]],
    current_account_ids: set[str],
) -> None:
    """Keep the browser manifest inside the owner-governed current perimeter.

    A derived sleeve epoch (#567) sits on ``<parent>-<sleeve>`` of a current
    account.  It is a product epoch of a required strategy, or a retained epoch
    of a nested history-only strategy; nothing else leaves the perimeter.
    """

    enforce_owner_perimeter = {
        "composer-holy-grail",
        "echotrade-growth-pivots",
    }.issubset(strategy_product_controls)
    history_only = _history_only_strategy_ids(strategy_product_controls)
    for row in _objects(account_manifest.get("rows"), "account_manifest.rows"):
        account_id = row.get("account_id")
        strategy_id = row.get("strategy_id")
        identity_is_text = isinstance(account_id, str) and isinstance(strategy_id, str)
        if not identity_is_text:
            raise ControlPanelValidationError(
                "current account manifest row is outside the product boundary"
            )
        account_id = cast(str, account_id)
        strategy_id = cast(str, strategy_id)
        if not enforce_owner_perimeter:
            continue
        derived = account_id not in current_account_ids and (
            _derived_epoch_parent(account_id, current_account_ids) is not None
        )
        required = (
            strategy_product_controls.get(strategy_id, {}).get("product_requirement")
            == "REQUIRED"
        )
        if not (
            (account_id in current_account_ids and required)
            or (derived and (required or strategy_id in history_only))
        ):
            raise ControlPanelValidationError(
                "current account manifest row is outside the product boundary"
            )


def _current_epoch_crosswalk_included_tuples(
    crosswalk: Mapping[str, Any],
) -> set[tuple[object, object, object, object]]:
    """Validate the schema-9 crosswalk and return its 13 included epochs."""
    if not isinstance(crosswalk, dict) or not all(
        isinstance(key, str) for key in crosswalk
    ):
        raise ControlPanelValidationError(
            "current epoch crosswalk violates formal schema: expected object"
        )
    required_crosswalk_keys = {
        "artifact_id",
        "as_of_date",
        "calculation_cutoff_date",
        "disposition_counts",
        "rows",
        "schema_version",
        "selected_baseline",
    }
    if set(crosswalk) != required_crosswalk_keys:
        raise ControlPanelValidationError(
            "current epoch crosswalk violates formal schema: root keys differ"
        )
    if (
        crosswalk.get("schema_version") != 1
        or crosswalk.get("artifact_id") != "account-strategy-current-epoch-crosswalk"
    ):
        raise ControlPanelValidationError(
            "current epoch crosswalk violates formal schema: identity differs"
        )

    def validate_iso_date(value: object, field_name: str) -> None:
        if not isinstance(value, str):
            raise ControlPanelValidationError(
                f"current epoch crosswalk violates formal schema at {field_name}: expected date"
            )
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ControlPanelValidationError(
                f"current epoch crosswalk violates formal schema at {field_name}: expected date"
            ) from exc
        if parsed.isoformat() != value:
            raise ControlPanelValidationError(
                f"current epoch crosswalk violates formal schema at {field_name}: expected canonical date"
            )

    validate_iso_date(crosswalk.get("as_of_date"), "as_of_date")
    validate_iso_date(
        crosswalk.get("calculation_cutoff_date"), "calculation_cutoff_date"
    )
    selected_baseline = crosswalk.get("selected_baseline")
    if not isinstance(selected_baseline, dict) or set(selected_baseline) != {
        "generation_id",
        "artifact_id",
        "sha256",
        "size_bytes",
        "as_of_date",
        "schema_name",
        "schema_version",
    }:
        raise ControlPanelValidationError(
            "current epoch crosswalk violates formal schema at selected_baseline: shape differs"
        )
    if (
        selected_baseline.get("artifact_id") != "normalized/portfolio-baseline.json"
        or selected_baseline.get("schema_name") != "portfolio-baseline-catalog"
        or selected_baseline.get("schema_version") not in {"1", "2"}
        or not isinstance(selected_baseline.get("generation_id"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", selected_baseline["generation_id"])
        or not isinstance(selected_baseline.get("sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", selected_baseline["sha256"])
        or type(selected_baseline.get("size_bytes")) is not int
        or selected_baseline["size_bytes"] < 1
    ):
        raise ControlPanelValidationError(
            "current epoch crosswalk violates formal schema at selected_baseline: values differ"
        )
    validate_iso_date(
        selected_baseline.get("as_of_date"), "selected_baseline.as_of_date"
    )

    if set(crosswalk) != {
        "artifact_id",
        "as_of_date",
        "calculation_cutoff_date",
        "disposition_counts",
        "rows",
        "schema_version",
        "selected_baseline",
    }:
        raise ControlPanelValidationError("current crosswalk shape is invalid")
    if (
        crosswalk.get("schema_version") != 1
        or crosswalk.get("artifact_id") != "account-strategy-current-epoch-crosswalk"
    ):
        raise ControlPanelValidationError("current crosswalk identity is invalid")
    for field in ("as_of_date", "calculation_cutoff_date"):
        value = crosswalk.get(field)
        if not isinstance(value, str):
            raise ControlPanelValidationError("current crosswalk dates are invalid")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ControlPanelValidationError(
                "current crosswalk dates are invalid"
            ) from exc
        if parsed.isoformat() != value:
            raise ControlPanelValidationError("current crosswalk dates are invalid")
    rows = _objects(crosswalk.get("rows"), "current crosswalk.rows")
    if len(rows) != 15:
        raise ControlPanelValidationError("current crosswalk must contain 15 rows")
    required_row_keys = {
        "account_id",
        "account_label",
        "assignment_id",
        "effective_end",
        "effective_start",
        "last_four",
        "owner_reason",
        "product_epoch_disposition",
        "provider",
        "strategy_id",
    }
    allowed_dispositions = {
        "INCLUDED_PRODUCT_EPOCH",
        "EXCLUDED_DEPRECATED_HISTORY_ONLY",
        "EXCLUDED_IMMATERIAL",
    }
    tuples: set[tuple[object, object, object, object]] = set()
    actual_counts: Counter[str] = Counter()
    for row in rows:
        if set(row) != required_row_keys:
            raise ControlPanelValidationError(
                "current epoch crosswalk violates formal schema at rows: shape differs"
            )
        if (
            not isinstance(row.get("account_id"), str)
            or not row["account_id"]
            or not isinstance(row.get("account_label"), str)
            or not row["account_label"]
            or not isinstance(row.get("assignment_id"), str)
            or not row["assignment_id"]
            or not isinstance(row.get("provider"), str)
            or not row["provider"]
            or not isinstance(row.get("strategy_id"), str)
            or not row["strategy_id"]
            or not isinstance(row.get("owner_reason"), str)
            or not row["owner_reason"]
            or (
                row.get("last_four") is not None
                and (
                    not isinstance(row.get("last_four"), str)
                    or not re.fullmatch(r"[0-9]{4}", row["last_four"])
                )
            )
        ):
            raise ControlPanelValidationError(
                "current epoch crosswalk violates formal schema at rows: field types differ"
            )
        disposition = row.get("product_epoch_disposition")
        if disposition not in allowed_dispositions:
            raise ControlPanelValidationError(
                "current epoch crosswalk violates formal schema at rows: disposition differs"
            )
        for field in ("effective_start", "effective_end"):
            value = row.get(field)
            if value is None and field == "effective_end":
                continue
            if not isinstance(value, str):
                raise ControlPanelValidationError("current crosswalk dates are invalid")
            try:
                parsed = date.fromisoformat(value)
            except ValueError as exc:
                raise ControlPanelValidationError(
                    "current crosswalk dates are invalid"
                ) from exc
            if parsed.isoformat() != value:
                raise ControlPanelValidationError("current crosswalk dates are invalid")
        epoch_tuple = (
            row.get("account_id"),
            row.get("strategy_id"),
            row.get("effective_start"),
            row.get("effective_end"),
        )
        if epoch_tuple in tuples:
            raise ControlPanelValidationError(
                "current crosswalk epoch tuples are not unique"
            )
        tuples.add(epoch_tuple)
        actual_counts[str(disposition)] += 1
    expected_counts = {
        "EXCLUDED_DEPRECATED_HISTORY_ONLY": 1,
        "EXCLUDED_IMMATERIAL": 1,
        "INCLUDED_PRODUCT_EPOCH": 13,
    }
    if (
        crosswalk.get("disposition_counts") != expected_counts
        or dict(actual_counts) != expected_counts
    ):
        raise ControlPanelValidationError(
            "current crosswalk disposition counts are invalid"
        )
    return {
        (
            row.get("account_id"),
            row.get("strategy_id"),
            row.get("effective_start"),
            row.get("effective_end"),
        )
        for row in rows
        if row.get("product_epoch_disposition") == "INCLUDED_PRODUCT_EPOCH"
    }


def _split_manifest_epoch_rows(
    rows: Sequence[Mapping[str, Any]],
    crosswalk_tuples: set[tuple[object, object, object, object]],
    strategy_product_controls: Mapping[str, Mapping[str, str]],
) -> tuple[
    list[Mapping[str, Any]],
    list[Mapping[str, Any]],
    set[tuple[object, object, object, object]],
]:
    """Split the manifest rows into product rows and retained derived rows (#567).

    A row is a crosswalk epoch, or a derived sleeve epoch on ``<parent>-<sleeve>``
    of a crosswalk account.  A derived epoch of a required strategy is a product
    row; one of a nested history-only strategy is retained; any other row is
    outside the perimeter.  A crosswalk epoch the manifest dropped must belong to
    an account that is the parent of derived rows.  Returns the product rows, the
    retained rows and the dropped crosswalk epochs.
    """

    crosswalk_accounts = {str(key[0]) for key in crosswalk_tuples}
    history_only = _history_only_strategy_ids(strategy_product_controls)
    product: list[Mapping[str, Any]] = []
    retained: list[Mapping[str, Any]] = []
    present: set[tuple[object, object, object, object]] = set()
    parents: set[str] = set()
    for row in rows:
        key = (
            row.get("account_id"),
            row.get("strategy_id"),
            row.get("effective_start"),
            row.get("effective_end"),
        )
        present.add(key)
        if key in crosswalk_tuples:
            product.append(row)
            continue
        account_id, strategy_id = key[0], key[1]
        parent = (
            _derived_epoch_parent(account_id, crosswalk_accounts)
            if isinstance(account_id, str) and account_id not in crosswalk_accounts
            else None
        )
        if parent is None or not isinstance(strategy_id, str):
            raise ControlPanelValidationError(
                "current manifest and performance identities differ"
            )
        parents.add(parent)
        if (
            strategy_product_controls.get(strategy_id, {}).get("product_requirement")
            == "REQUIRED"
        ):
            product.append(row)
        elif strategy_id in history_only:
            retained.append(row)
        else:
            raise ControlPanelValidationError(
                "current manifest and performance identities differ"
            )
    removed = crosswalk_tuples - present
    if any(str(key[0]) not in parents for key in removed):
        raise ControlPanelValidationError(
            "current manifest and performance identities differ"
        )
    return product, retained, removed


def _validate_current_product_epoch_surface(
    account_manifest: Mapping[str, Any],
    performance: Sequence[Mapping[str, Any]],
    strategy_product_controls: Mapping[str, Mapping[str, str]],
    *,
    as_of_date: str | None = None,
    current_epoch_crosswalk: Mapping[str, Any] | None = None,
) -> set[tuple[str, str]]:
    """Require exact current account-strategy identities across two surfaces."""

    crosswalk_tuples: set[tuple[object, object, object, object]] | None = None
    if account_manifest.get("schema_version") in {9, 10}:
        if current_epoch_crosswalk is None:
            raise ControlPanelValidationError(
                "current manifest and performance identities differ"
            )
        crosswalk_tuples = _current_epoch_crosswalk_included_tuples(
            current_epoch_crosswalk
        )
        if CURRENT_PRODUCT_STRATEGY_IDS <= set(strategy_product_controls):
            excluded_rows = [
                row
                for row in _objects(
                    current_epoch_crosswalk.get("rows"), "current crosswalk.rows"
                )
                if row.get("product_epoch_disposition") != "INCLUDED_PRODUCT_EPOCH"
            ]
            if {
                row.get("strategy_id") for row in excluded_rows
            } != CURRENT_EXCLUDED_STRATEGY_IDS or {
                row.get("product_epoch_disposition") for row in excluded_rows
            } != {
                "EXCLUDED_DEPRECATED_HISTORY_ONLY",
                "EXCLUDED_IMMATERIAL",
            }:
                raise ControlPanelValidationError(
                    "current crosswalk excluded strategy boundary differs"
                )
            excluded_by_strategy = {row["strategy_id"]: row for row in excluded_rows}
            if (
                excluded_by_strategy["echotrade-growth-pivots"]["effective_end"]
                != "2025-11-24"
                or excluded_by_strategy["composer-holy-grail"][
                    "product_epoch_disposition"
                ]
                != "EXCLUDED_IMMATERIAL"
            ):
                raise ControlPanelValidationError(
                    "current crosswalk excluded strategy boundary differs"
                )

    product_manifest_rows: list[Mapping[str, Any]] | None = None
    if crosswalk_tuples is not None:
        # Derived sleeves (#567) replace a residual parent's epoch: the product
        # rows are the crosswalk's epochs that remain plus the sleeve epochs of
        # required strategies; sleeve epochs of history-only strategies stay in
        # the manifest as retained rows and are no product surface.
        product_manifest_rows, _retained_rows, removed = _split_manifest_epoch_rows(
            _objects(account_manifest.get("rows"), "account_manifest.rows"),
            crosswalk_tuples,
            strategy_product_controls,
        )
        product_tuples = {
            tuple(
                row.get(field)
                for field in (
                    "account_id",
                    "strategy_id",
                    "effective_start",
                    "effective_end",
                )
            )
            for row in product_manifest_rows
        }
        if product_tuples != (crosswalk_tuples - removed) | {
            key for key in product_tuples if key not in crosswalk_tuples
        }:
            raise ControlPanelValidationError(
                "current manifest and performance identities differ"
            )
        for surface, field_name in (
            (product_manifest_rows, "account_manifest.rows"),
            (performance, "performance"),
        ):
            surface_rows = _objects(surface, field_name)
            surface_tuples = {
                tuple(
                    row.get(field)
                    for field in (
                        "account_id",
                        "strategy_id",
                        "effective_start",
                        "effective_end",
                    )
                )
                for row in surface_rows
            }
            if (
                len(surface_rows) != CURRENT_PRODUCT_EPOCH_COUNT
                or surface_tuples != product_tuples
            ):
                raise ControlPanelValidationError(
                    "current manifest and performance identities differ"
                )

    # Synthetic panel fixtures intentionally use a private strategy namespace;
    # production/verified controls are the only authority to which the exact
    # thirteen-row current epoch rule applies.
    if not set(strategy_product_controls) & CURRENT_REQUIRED_STRATEGY_IDS:
        return set()

    boundary_text = as_of_date or "2026-08-13"
    if (
        account_manifest.get("schema_version") in {9, 10}
        and current_epoch_crosswalk is not None
    ):
        boundary_text = _text(
            current_epoch_crosswalk.get("calculation_cutoff_date"),
            "current crosswalk calculation_cutoff_date",
        )
    try:
        boundary_date = date.fromisoformat(boundary_text)
    except ValueError as exc:
        raise ControlPanelValidationError(
            "current manifest and performance identities differ"
        ) from exc

    def validate_row_dates(row: Mapping[str, Any]) -> None:
        for field in (
            "as_of_date",
            "effective_start",
            "effective_end",
            "start",
            "end",
            "observed_start",
            "observed_end",
            "return_path_observed_start",
            "return_path_observed_end",
        ):
            value = row.get(field)
            if value is None:
                continue
            if not isinstance(value, str):
                raise ControlPanelValidationError(
                    "current manifest and performance identities differ"
                )
            try:
                parsed = date.fromisoformat(value)
                if parsed.isoformat() != value or parsed > boundary_date:
                    raise ControlPanelValidationError(
                        "current manifest and performance identities differ"
                    )
            except ValueError as exc:
                raise ControlPanelValidationError(
                    "current manifest and performance identities differ"
                ) from exc

    manifest_rows = (
        list(product_manifest_rows)
        if product_manifest_rows is not None
        else _objects(account_manifest.get("rows"), "account_manifest.rows")
    )
    if len(manifest_rows) != CURRENT_PRODUCT_EPOCH_COUNT:
        raise ControlPanelValidationError(
            "current account manifest must contain exactly 13 rows"
        )
    # The pair alone is not a sufficient identity: it allows a forged
    # thirteen-row surface to move an account/strategy mapping across epochs.
    # Only effective dates are part of that shared identity. Manifest
    # calculation intervals (start/end) and report observation intervals
    # (observed_start/observed_end) are intentionally different concepts and
    # are validated independently above rather than falsely equated.
    performance_rows = _objects(performance, "performance")
    date_fields = ("effective_start", "effective_end")

    expected: set[tuple[str, str]] = set()
    expected_epoch_tuples: set[tuple[object, object, object, object]] = set()
    expected_full: set[tuple[object, ...]] = set()
    manifest_strategy_ids: set[str] = set()
    for row in manifest_rows:
        account_id = row.get("account_id")
        strategy_id = row.get("strategy_id")
        if not isinstance(account_id, str) or not isinstance(strategy_id, str):
            raise ControlPanelValidationError(
                "current manifest and performance identities differ"
            )
        if strategy_id not in CURRENT_REQUIRED_STRATEGY_IDS:
            raise ControlPanelValidationError(
                "current manifest and performance identities differ"
            )
        key = (account_id, strategy_id)
        if key in expected:
            raise ControlPanelValidationError(
                "current manifest and performance identities differ"
            )
        validate_row_dates(row)
        expected.add(key)
        manifest_strategy_ids.add(strategy_id)
        expected_full.add(
            (account_id, strategy_id, *(row.get(field) for field in date_fields))
        )
        expected_epoch_tuples.add(
            (
                row.get("account_id"),
                row.get("strategy_id"),
                row.get("effective_start"),
                row.get("effective_end"),
            )
        )
    if crosswalk_tuples is not None and expected_epoch_tuples != product_tuples:
        raise ControlPanelValidationError(
            "current manifest and performance identities differ"
        )
    if manifest_strategy_ids != CURRENT_REQUIRED_STRATEGY_IDS:
        raise ControlPanelValidationError(
            "current manifest and performance identities differ"
        )

    if len(performance_rows) != CURRENT_PRODUCT_EPOCH_COUNT:
        raise ControlPanelValidationError(
            "current performance must contain exactly 13 rows"
        )
    actual: set[tuple[str, str]] = set()
    actual_full: set[tuple[object, ...]] = set()
    performance_strategy_ids: set[str] = set()
    for row in performance_rows:
        account_id = row.get("account_id")
        strategy_id = row.get("strategy_id")
        if not isinstance(account_id, str) or not isinstance(strategy_id, str):
            raise ControlPanelValidationError(
                "current manifest and performance identities differ"
            )
        key = (account_id, strategy_id)
        if key in actual or key not in expected:
            raise ControlPanelValidationError(
                "current manifest and performance identities differ"
            )
        actual.add(key)
        validate_row_dates(row)
        actual_full.add(
            (account_id, strategy_id, *(row.get(field) for field in date_fields))
        )
        performance_strategy_ids.add(strategy_id)
    if performance_strategy_ids != CURRENT_REQUIRED_STRATEGY_IDS:
        raise ControlPanelValidationError(
            "current manifest and performance identities differ"
        )
    if actual_full != expected_full:
        raise ControlPanelValidationError(
            "current manifest and performance identities differ"
        )
    if actual != expected:
        raise ControlPanelValidationError(
            "current manifest and performance identities differ"
        )
    return expected


def _performance_lineage(
    account_manifest: Mapping[str, Any] | None,
    baseline_catalog: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare the report/baseline ledger with the manifest calculation ledger."""

    if account_manifest is None or account_manifest.get("schema_version") not in {
        9,
        10,
    }:
        return {"status": "NOT_APPLICABLE"}
    baseline_candidates = [
        row
        for row in baseline_catalog.get("source_artifacts", [])
        if isinstance(row, Mapping)
        and row.get("schema_name") == "provider-ledger-catalog"
    ]
    manifest_artifact = _object(
        _object(
            _object(
                account_manifest.get("source_identities"),
                "account manifest source_identities",
            ).get("ledger"),
            "account manifest ledger identity",
        ).get("artifact"),
        "account manifest ledger artifact",
    )
    manifest_schema = _object(
        manifest_artifact.get("schema"), "account manifest ledger schema"
    )
    manifest_identity = {
        "schema_name": _text(
            manifest_schema.get("name"), "account manifest ledger schema name"
        ),
        "schema_version": _text(
            manifest_schema.get("version"), "account manifest ledger schema version"
        ),
        "sha256": _text(
            manifest_artifact.get("sha256"), "account manifest ledger sha256"
        ),
    }
    if len(baseline_candidates) != 1:
        baseline_identity: dict[str, str] = {
            "status": "UNAVAILABLE_BASELINE_PROVIDER_LEDGER_IDENTITY"
        }
    else:
        baseline_row = baseline_candidates[0]
        baseline_identity = {
            "schema_name": _text(
                baseline_row.get("schema_name"), "baseline provider-ledger schema name"
            ),
            "schema_version": _text(
                baseline_row.get("schema_version"),
                "baseline provider-ledger schema version",
            ),
            "sha256": _text(
                baseline_row.get("sha256"), "baseline provider-ledger sha256"
            ),
        }
    return {
        "baseline_provider_ledger": baseline_identity,
        "manifest_provider_ledger": manifest_identity,
        "status": (
            "PASS"
            if baseline_identity == manifest_identity
            else "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH"
        ),
    }


def _suppress_unmatched_report_performance(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Preserve epoch identity while removing numerics from unmatched lineage."""

    suppressed: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        for field in (
            "flow_count",
            "flow_timing",
            "mwr_xirr",
            "mwr_opening_date",
            "mwr_opening_value",
            "mwr_terminal_date",
            "mwr_terminal_value",
            "mwr_currency",
            "mwr_label",
            "mwr_value_basis",
            "mwr_valuation_timing",
            "mwr_day_count",
            "mwr_unit",
            "mwr_source_binding",
            "nav_observations",
            "observed_start",
            "observed_end",
            "return_path_account_binding",
            "return_path_assignment_id",
            "return_path_authority",
            "return_path_cumulative_return",
            "return_path_evidence_layer",
            "return_path_generation_id",
            "return_path_observations",
            "return_path_observed_start",
            "return_path_observed_end",
            "return_path_series_sha256",
            "twr",
        ):
            row[field] = None
        row["mwr_reason"] = "SOURCE_LINEAGE_MISMATCH"
        row["mwr_coverage_status"] = "BLOCKED"
        row["mwr_cashflows"] = []
        for field in ("twr_status", "mwr_status", "return_path_status"):
            row[field] = "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH"
        suppressed.append(row)
    return suppressed


def _validate_twr_payload(row: Mapping[str, Any]) -> None:
    """Validate the manifest-bound TWR payload a report row carries.

    The report TWR is the epoch manifest's own engine result; the panel
    never recomputes it, so an available TWR must arrive with the epoch
    observation facts it was linked over, and an unavailable TWR must
    carry no numeric value.
    """

    twr = row.get("twr")
    twr_status = row.get("twr_status")
    if str(twr_status).startswith("AVAILABLE"):
        if not isinstance(twr, str) or not twr:
            raise ControlPanelValidationError("available TWR payload is incomplete")
        nav_observations = row.get("nav_observations")
        if not isinstance(nav_observations, int) or nav_observations < 1:
            raise ControlPanelValidationError("available TWR payload is incomplete")
        for field in ("observed_start", "observed_end"):
            value = row.get(field)
            if not isinstance(value, str):
                raise ControlPanelValidationError("available TWR payload is incomplete")
            try:
                if date.fromisoformat(value).isoformat() != value:
                    raise ControlPanelValidationError(
                        "available TWR payload is incomplete"
                    )
            except ValueError as exc:
                raise ControlPanelValidationError(
                    "available TWR payload is incomplete"
                ) from exc
        flow_count = row.get("flow_count")
        if flow_count is not None and (
            not isinstance(flow_count, int) or flow_count < 0
        ):
            raise ControlPanelValidationError("available TWR payload is incomplete")
    elif twr is not None:
        raise ControlPanelValidationError("unavailable TWR must carry no value")


def _validate_mwr_perimeter(rows: Sequence[Mapping[str, Any]]) -> None:
    """Keep the panel a strict projection of governed report TWR/MWR facts."""
    for row in rows:
        _validate_twr_payload(row)
        status = row.get("mwr_status")
        if status == "AVAILABLE":
            required = (
                "mwr_xirr",
                "mwr_opening_date",
                "mwr_opening_value",
                "mwr_terminal_date",
                "mwr_terminal_value",
                "mwr_currency",
                "mwr_coverage_status",
                "mwr_cashflows",
                "mwr_label",
                "mwr_value_basis",
                "mwr_valuation_timing",
                "mwr_day_count",
                "mwr_unit",
                "mwr_source_binding",
            )
            if any(row.get(field) is None for field in required):
                raise ControlPanelValidationError("available MWR payload is incomplete")
            if row.get("mwr_coverage_status") != "COMPLETE":
                raise ControlPanelValidationError(
                    "available MWR coverage is incomplete"
                )
            basis = row.get("mwr_value_basis")
            label = row.get("mwr_label")
            if (basis == "STRATEGY_NAV") != (label == "STRATEGY_EPOCH_MWR"):
                raise ControlPanelValidationError(
                    "MWR label differs from valuation perimeter"
                )
            cashflows = _objects(row.get("mwr_cashflows"), "MWR cashflows")
            if not cashflows or any(
                not isinstance(flow.get("source_record_ids"), list)
                or not flow["source_record_ids"]
                or not all(
                    isinstance(source_id, str) and source_id
                    for source_id in flow["source_record_ids"]
                )
                for flow in cashflows
            ):
                raise ControlPanelValidationError(
                    "MWR cashflow source identities are absent"
                )
        elif status not in {
            "UNAVAILABLE_INCOMPLETE_FLOW_COVERAGE",
            "BLOCKED_ADMISSION",
        }:
            if row.get("mwr_xirr") is not None or not isinstance(
                row.get("mwr_reason"), str
            ):
                raise ControlPanelValidationError(
                    "unavailable MWR must retain reason and null rate"
                )


def _suppress_unmatched_strategy_performance(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Remove report-derived account-live metrics from an unmatched ledger."""

    suppressed: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        for field in (
            "account_live_cagr",
            "account_live_cumulative_return",
            "account_live_max_drawdown",
            "account_live_observed_end",
            "account_live_observed_start",
            "account_live_return_authority",
            "account_live_return_observations",
            "account_live_return_series_schema",
            "account_live_return_series_sha256",
            "account_live_source_generation",
            "account_live_source_generation_as_of_date",
            "account_live_source_provider",
            "account_live_source_sha256",
            "available_twr_segments",
            "mwr_xirr",
            "mwr_opening_date",
            "mwr_terminal_date",
            "mwr_member_account_ids",
            "current_value_usd",
            "hwm_current_drawdown",
            "hwm_freshness_status",
            "hwm_max_drawdown",
            "hwm_source",
            "nav_evidence_layer",
            "nav_observed_end",
            "nav_observed_start",
            "nav_return_observations",
            "benchmark_id",
            "benchmark_effective_start",
            "benchmark_effective_end",
            "benchmark_version_ref",
            "benchmark_rationale",
            "benchmark_comparison_observations",
            "benchmark_comparison_observed_start",
            "benchmark_comparison_observed_end",
            "benchmark_account_cumulative_return",
            "benchmark_reference_cumulative_return",
            "benchmark_cumulative_gap",
            "benchmark_active_return_annualized",
            "benchmark_tracking_error_annualized",
            "benchmark_information_ratio",
            "benchmark_beta",
            "benchmark_correlation",
            "benchmark_upside_capture",
            "benchmark_downside_capture",
            "benchmark_relative_drawdown",
            "provider_comparison_observed_end",
            "provider_comparison_observed_start",
            "provider_comparison_observations",
            "provider_reference_cumulative_return",
            "provider_account_cumulative_return",
            "provider_cumulative_gap",
            "provider_active_return_annualized",
            "provider_beta",
            "provider_correlation",
            "provider_tracking_error_annualized",
            "provider_information_ratio",
            "provider_upside_capture",
            "provider_downside_capture",
            "provider_relative_drawdown",
            "provider_comparison_version_compatibility",
            "cash_drag_status",
            "cash_drag_reason",
            "legacy_baseline_disposition",
            "annual_external_cost_rate_pre_tax",
            "cost_rate_status",
            "cost_rate_reason",
            "cost_rate_denominator_usd",
            "cost_rate_denominator_as_of_date",
            "cost_rate_denominator_basis",
            "cost_schedule_as_of_date",
            "annual_external_cost_usd",
            "after_tax_annual_cost_rate",
            "after_tax_status",
            "fees_status",
            "cost_class",
            "declared",
            "corroboration",
            "execution_cost",
            "systematic_weight",
        ):
            row[field] = None
        row["account_live_cagr_status"] = "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH"
        row["account_live_status"] = "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH"
        row["nav_status"] = "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH"
        row["benchmark_assignment_status"] = "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH"
        row["benchmark_comparison_status"] = "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH"
        row["benchmark_comparison_disposition"] = "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH"
        row["provider_comparison_status"] = "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH"
        row["provider_comparison_disposition"] = "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH"
        suppressed.append(row)
    return suppressed


def _unavailable_lineage_hwm_summary(policy: Mapping[str, Any]) -> dict[str, Any]:
    """Emit a schema-valid HWM state when its performance lineage is rejected."""

    return {
        "covered_value_usd": None,
        "excluded_details": [],
        "excluded_strategies": [],
        "excluded_value_usd": None,
        "governed_perimeter_value_usd": None,
        "hwm_numerical_status": "UNAVAILABLE",
        "hwm_numerical_reason": "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH",
        "nav_coverage": None,
        "policy_id": policy["policy_id"],
        "status": "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH",
        "status_code_version": HWM_STATUS_CODE_VERSION,
        "status_semantics": HWM_STATUS_SEMANTICS,
        "uncovered_details": [],
        "uncovered_strategies": [],
        "uncovered_value_usd": None,
    }


def _current_product_account_ids_from_baseline(
    baseline_catalog: Mapping[str, Any],
    strategy_product_controls: Mapping[str, Mapping[str, str]],
) -> set[str]:
    """Derive current account membership from the governed baseline only."""

    account_ids = {
        _text(account.get("account_id"), "baseline account_id")
        for account in _objects(baseline_catalog.get("accounts"), "baseline accounts")
        if account.get("role") == "RESERVE"
    }
    account_ids.update(
        _text(allocation.get("account_id"), "reserve allocation account_id")
        for allocation in _objects(
            baseline_catalog.get("allocations"), "baseline allocations"
        )
        if allocation.get("scope") == "RESERVE"
        and isinstance(allocation.get("account_id"), str)
    )
    for assignment in _objects(
        baseline_catalog.get("assignments"), "baseline assignments"
    ):
        if assignment.get("effective_end") is not None:
            continue
        mapping_status = assignment.get(
            "mapping_status", assignment.get("strategy_mapping_disposition")
        )
        if mapping_status not in {"MAPPED", "STRATEGY_MAPPED"}:
            continue
        strategy_id = assignment.get("strategy_id")
        if not isinstance(strategy_id, str):
            continue
        if (
            strategy_product_controls.get(strategy_id, {}).get("product_requirement")
            != "REQUIRED"
        ):
            continue
        account_ids.add(_text(assignment.get("account_id"), "assignment.account_id"))
    return account_ids


def _current_product_account_summary_rows(
    rows: Sequence[Mapping[str, Any]],
    baseline_catalog: Mapping[str, Any],
    strategy_product_controls: Mapping[str, Mapping[str, str]],
) -> list[dict[str, Any]]:
    """Build the browser-facing account selector from baseline membership."""

    current_account_ids = _current_product_account_ids_from_baseline(
        baseline_catalog, strategy_product_controls
    )
    return [
        dict(row)
        for row in rows
        if isinstance(row.get("account_id"), str)
        and row["account_id"] in current_account_ids
    ]


def _summary(report: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    rows = _objects(report.get("portfolio_summary"), "portfolio_summary")
    return {_text(row.get("metric"), "summary.metric"): row for row in rows}


def _summary_value(summary: Mapping[str, Mapping[str, Any]], metric: str) -> str:
    row = summary.get(metric)
    if row is None:
        raise ControlPanelValidationError(f"portfolio summary lacks {metric}")
    return _text(row.get("value"), f"portfolio_summary.{metric}")


_CAPITAL_AVAILABLE: Final = "AVAILABLE"
_CAPITAL_NOT_READY: Final = "NOT_READY_AT_CUTOFF"
_CAPITAL_COMPONENTS: Final = (
    ("reserve", "Reserve"),
    ("retained_excluded", "Retained excluded systematic balance"),
    ("systematic", "Systematic capital"),
    ("total_liquid", "Total liquid"),
)
WEIGHT_BASIS_CURRENT: Final = "CURRENT_CUTOFF"
WEIGHT_BASIS_POLICY: Final = "POLICY_PORT_DEPENDENCE_1"
DEPENDENCE_POLICY_ID: Final = "PORT-DEPENDENCE-1"
DEPENDENCE_WEIGHT_NORMALIZATION: Final = (
    "NORMALIZE_WITHIN_OWNER_APPROVED_REQUIRED_DEPENDENCE_PERIMETER"
)


def _capital_decimal(value: object, field_name: str) -> Decimal:
    if not isinstance(value, str):
        raise ControlPanelValidationError(f"{field_name} must be decimal text")
    try:
        accepted = Decimal(value)
    except InvalidOperation as exc:
        raise ControlPanelValidationError(f"{field_name} must be decimal text") from exc
    if not accepted.is_finite():
        raise ControlPanelValidationError(f"{field_name} must be finite")
    return accepted


def _exact_capital_sum(values: Sequence[Decimal]) -> Decimal:
    """Add money exactly: the default 28-digit context would round long sums."""

    with localcontext() as context:
        context.prec = 100
        return sum(values, Decimal(0))


def _capital_summary(
    summary: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, str | None]]:
    """The panel capital block, from the report's portfolio summary rows.

    ``capital`` keeps decimal-or-null fields; ``capital_status`` and ``capital_as_of``
    are its siblings.  A component that is not ready at the cutoff is null with its
    status, never a stand-in value and never a remainder of another figure.
    """

    capital: dict[str, str | None] = {}
    status: dict[str, str | None] = {}
    as_of: dict[str, str | None] = {}
    for key, metric in _CAPITAL_COMPONENTS:
        row = summary.get(metric)
        if row is None:
            raise ControlPanelValidationError(f"portfolio summary lacks {metric}")
        state = row.get("status")
        if state not in (_CAPITAL_AVAILABLE, _CAPITAL_NOT_READY):
            raise ControlPanelValidationError(
                f"portfolio summary capital status is invalid: {metric}"
            )
        raw = row.get("value")
        value = None if raw is None else _text(raw, f"portfolio_summary.{metric}")
        if value is not None:
            _capital_decimal(value, f"portfolio_summary.{metric}")
        if (state == _CAPITAL_AVAILABLE) != (value is not None):
            raise ControlPanelValidationError(
                f"portfolio summary capital status contradicts its value: {metric}"
            )
        day = row.get("as_of_date")
        if value is None and day is not None:
            raise ControlPanelValidationError(
                f"portfolio summary capital is not ready but dated: {metric}"
            )
        if day is not None:
            day = _text(day, f"portfolio_summary.{metric}.as_of_date")
            try:
                date.fromisoformat(day)
            except ValueError as exc:
                raise ControlPanelValidationError(
                    f"portfolio_summary.{metric}.as_of_date must be an ISO date"
                ) from exc
        capital[key] = value
        status[key] = state
        as_of[key] = day
    return {"capital": capital, "capital_as_of": as_of, "capital_status": status}


def _capital_identity_problems(data: Mapping[str, Any]) -> list[str]:
    """Recompute the capital identity from the panel's own rows.

    Systematic capital is the exact sum of the strategy rows; total liquid is
    systematic plus reserve plus retained excluded, current only when all three are;
    a component that is not ready makes every figure that needs it not ready.  The
    check reads the published block and the published strategy rows, never the
    report's summary rows that fed the block.
    """

    problems: list[str] = []
    capital = _object(data.get("capital"), "panel capital")
    status = _object(data.get("capital_status"), "panel capital status")
    as_of = _object(data.get("capital_as_of"), "panel capital as_of")
    values: dict[str, Decimal | None] = {}
    for key, _metric in _CAPITAL_COMPONENTS:
        raw = capital.get(key)
        try:
            value = None if raw is None else Decimal(str(raw))
        except InvalidOperation:
            value = None
            problems.append(f"capital value is not decimal for {key}")
        values[key] = value
        state = status.get(key)
        if state not in (_CAPITAL_AVAILABLE, _CAPITAL_NOT_READY) or (
            (state == _CAPITAL_AVAILABLE) != (value is not None)
        ):
            problems.append(f"capital status contradicts its value for {key}")
        if value is None and as_of.get(key) is not None:
            problems.append(f"capital is not ready but dated for {key}")
    strategy_values = [
        row.get("current_value_usd")
        for row in _current_strategy_rows(data, "panel strategies")
    ]
    if strategy_values and all(item is not None for item in strategy_values):
        strategy_total = _exact_capital_sum(
            [Decimal(str(item)) for item in strategy_values]
        )
        if values["systematic"] is None:
            problems.append(
                "systematic capital is not ready although the strategy rows are current"
            )
        elif values["systematic"] != strategy_total:
            problems.append(
                "systematic capital differs from the sum of the strategy rows"
            )
    components = [values["systematic"], values["reserve"], values["retained_excluded"]]
    if all(item is not None for item in components):
        expected = _exact_capital_sum([item for item in components if item is not None])
        if values["total_liquid"] is None or values["total_liquid"] != expected:
            problems.append(
                "total liquid differs from systematic plus reserve plus retained excluded"
            )
    elif values["total_liquid"] is not None:
        problems.append(
            "total liquid is published although a not ready component exists"
        )
    return problems


def _reconcile_capital_with_baseline(
    parts: Mapping[str, Mapping[str, str | None]],
    baseline_catalog: Mapping[str, Any],
) -> None:
    """The report's capital must be the governed baseline's current capital.

    The report derives its capital rows from the same baseline block; this is the
    panel's own read of the governed baseline, so the panel no longer compares the
    report's capital rows with themselves.
    """

    raw = baseline_catalog.get("current_capital")
    if raw is None:
        raise ControlPanelValidationError("baseline catalog lacks current_capital")
    block = _object(raw, "baseline current_capital")
    for key, _metric in _CAPITAL_COMPONENTS:
        part = _object(block.get(key), f"baseline current_capital.{key}")
        amount = _object(part.get("current_value"), f"baseline {key} current_value")
        baseline_value = amount.get("value")
        observed_value = parts["capital"][key]
        same_value = (baseline_value is None) == (observed_value is None) and (
            baseline_value is None
            or _capital_decimal(baseline_value, f"baseline {key} value")
            == _capital_decimal(observed_value, f"report {key} value")
        )
        if (
            not same_value
            or part.get("status") != parts["capital_status"][key]
            or part.get("as_of_date") != parts["capital_as_of"][key]
        ):
            raise ControlPanelValidationError(
                "report capital differs from the governed baseline current capital"
            )


def _dependence_weight_authority() -> dict[str, str]:
    """The authority of the policy weights behind the dependence components."""

    policy = _load_json(DEPENDENCE_POLICY, "dependence policy")
    if policy.get("policy_id") != DEPENDENCE_POLICY_ID:
        raise ControlPanelValidationError("dependence policy identity differs")
    authority = _object(policy.get("allocation_authority"), "dependence authority")
    day = _text(authority.get("as_of_date"), "dependence authority as_of_date")
    try:
        date.fromisoformat(day)
    except ValueError as exc:
        raise ControlPanelValidationError(
            "dependence authority as_of_date must be an ISO date"
        ) from exc
    return {
        "as_of_date": day,
        "normalization": DEPENDENCE_WEIGHT_NORMALIZATION,
        "policy_id": DEPENDENCE_POLICY_ID,
    }


def _report_panel_expectations(report: Mapping[str, Any]) -> dict[str, Any]:
    summary = _summary(report)
    counts = {
        "accounts": len(_objects(report.get("account_summary"), "account_summary")),
        "allocations": len(
            _objects(report.get("allocation_history"), "allocation_history")
        ),
        "strategies": len(_objects(report.get("strategy_summary"), "strategy_summary")),
    }
    if any(count == 0 for count in counts.values()):
        raise ControlPanelValidationError(
            "accepted report portfolio dimensions must be non-empty"
        )
    result = {**_capital_summary(summary), "counts": counts}
    custody = report.get("custody_current_capital")
    if custody is not None:
        from portfolio_report import _current_capital

        try:
            _current_capital({"current_capital": custody})
        except (RuntimeError, ValueError) as exc:
            raise ControlPanelValidationError(
                f"custody current capital: {exc}"
            ) from exc
        if not isinstance(custody, dict) or not custody.get("custody_authority"):
            raise ControlPanelValidationError("custody cash lacks admitted authority")
        result["custody_current_capital"] = custody
    return result


def _fact_rows(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    return _objects(report.get("coverage_data_quality"), "coverage_data_quality")


def _finite_float(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ControlPanelValidationError(f"{field_name} must be numeric")
    try:
        accepted = float(value)
    except ValueError as exc:
        raise ControlPanelValidationError(f"{field_name} must be numeric") from exc
    if not math.isfinite(accepted):
        raise ControlPanelValidationError(f"{field_name} must be finite")
    return accepted


def _number_text(value: float) -> str:
    if abs(value) < 5e-16:
        value = 0.0
    return format(value, ".12g")


def _hwm_policy(
    evaluation_date: date, policy_snapshot: bytes | None = None
) -> tuple[dict[str, Any], str]:
    if policy_snapshot is None:
        policy, _policy_content, policy_hash = _json_snapshot(HWM_POLICY, "HWM policy")
    else:
        policy = _json_from_bytes(policy_snapshot, "HWM policy")
        policy_hash = hashlib.sha256(policy_snapshot).hexdigest()
    if policy.get("schema_version") != 2 or policy.get("policy_id") != "PORT-HWM-3":
        raise ControlPanelValidationError("HWM policy identity is unsupported")
    perimeter = _object(policy.get("material_perimeter"), "material_perimeter")
    exclusions = _objects(perimeter.get("exclusions"), "material_perimeter.exclusions")
    excluded_ids: set[str] = set()
    for item in exclusions:
        strategy_id = _text(item.get("strategy_id"), "HWM exclusion strategy_id")
        if strategy_id in excluded_ids:
            raise ControlPanelValidationError("HWM exclusion is duplicated")
        if item.get("classification") != "EXCLUDED_DE_MINIMIS":
            raise ControlPanelValidationError("HWM exclusion classification differs")
        effective_date = date.fromisoformat(
            _text(item.get("effective_date"), "HWM exclusion effective_date")
        )
        if effective_date <= evaluation_date:
            excluded_ids.add(strategy_id)
    accepted_evidence = policy.get("accepted_daily_nav_evidence")
    if not isinstance(accepted_evidence, list) or not all(
        isinstance(item, str) and item for item in accepted_evidence
    ):
        raise ControlPanelValidationError("HWM accepted evidence differs")
    accepted_provider_live = policy.get("accepted_provider_live_strategy_ids")
    if not isinstance(accepted_provider_live, list) or not all(
        isinstance(item, str) and item for item in accepted_provider_live
    ):
        raise ControlPanelValidationError("HWM provider-live allowlist differs")
    policy["excluded_strategy_ids"] = sorted(excluded_ids)
    policy["evaluation_date"] = evaluation_date.isoformat()
    return policy, policy_hash


def _governed_hwm_coverage(
    strategies: Sequence[Mapping[str, Any]],
    policy: Mapping[str, Any],
    freshness_health: Sequence[Mapping[str, Any]] | None = None,
    *,
    require_freshness: bool = False,
    calculation_boundary: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        return governed_hwm_coverage(
            strategies,
            policy,
            freshness_health,
            require_freshness=require_freshness,
            calculation_boundary=calculation_boundary,
        )
    except HwmCoverageError as exc:
        raise ControlPanelValidationError(str(exc)) from exc


def _effective_hwm_policy_for_current_strategies(
    strategies: Sequence[Mapping[str, Any]], policy: Mapping[str, Any]
) -> dict[str, Any]:
    """Project full HWM policy exclusions onto the displayed current perimeter."""

    current_strategy_ids = {
        _text(row.get("strategy_id"), "strategy_summary.strategy_id")
        for row in strategies
    }
    policy_excluded_strategy_ids = set(policy["excluded_strategy_ids"])
    required_policy_exclusions = policy_excluded_strategy_ids & current_strategy_ids
    if required_policy_exclusions:
        raise ControlPanelValidationError(
            "HWM policy excludes a required current-product strategy"
        )
    effective_hwm_policy = dict(policy)
    effective_hwm_policy["excluded_strategy_ids"] = sorted(
        policy_excluded_strategy_ids & current_strategy_ids
    )
    return effective_hwm_policy


def _validate_panel_hwm_envelope(
    envelope: Mapping[str, Any], catalog: Mapping[str, Any]
) -> None:
    """Enforce the FULL hwm_inputs envelope contract against the governed
    baseline catalog before the panel trusts the envelope for recomputation.

    The catalog is the hash-verified authority (resolved by the report's
    baseline reference); this is the panel's second trust boundary.  The
    shared consumption-boundary validator
    (:func:`quant_engine.portfolio_ledger.validate_hwm_inputs_envelope`) is
    the single authority — the same contract the baseline builder enforces
    at admission — covering the closed shapes, anchor constants and typed
    acceptance provenance, per-lane calendar authorities with artifact-role
    resolution, the reconciliation block (NAV_ROLLFORWARD with a reconciled
    zero-delta endpoint), period-row enums and disposition invariants,
    attestation evidence shape and role resolution, EXCLUDED_BY_POLICY rows
    against the admitted PORT-HWM-3 exclusions, the governed anchor-to-as-of
    keyspace over the systematic perimeter, and per-AVAILABLE-row
    capital/component identity against the hash-bound rollforward rows.  Any
    violation fails closed with an error — the envelope is never silently
    trusted.
    """
    artifact_roles: dict[str, tuple[str, str]] = {}
    for link in _objects(catalog.get("source_artifacts"), "source_artifacts"):
        artifact_id = _text(link.get("artifact_id"), "source_artifact.artifact_id")
        artifact_roles[artifact_id] = (
            _text(link.get("schema_name"), "source_artifact.schema_name"),
            _text(link.get("sha256"), "source_artifact.sha256"),
        )
    # Consumption-boundary full context: the builder embeds the hash-verified
    # acceptance record and allocations snapshot into the catalog so the panel
    # can enforce the anchor-content cross-check and endpoint recomputation
    # without access to the governed source files.  Missing context is a
    # fail-closed condition — the panel never falls back to declaration-only
    # mode.  Both contexts are validated before ANY use of catalog allocations
    # (including perimeter derivation below).
    hwm_acceptance_record = catalog.get("hwm_acceptance_record")
    hwm_allocations = catalog.get("allocations")
    if not isinstance(hwm_acceptance_record, dict) or not isinstance(
        hwm_allocations, list
    ):
        raise ControlPanelValidationError(
            "hwm_inputs consumption context is incomplete: "
            "hwm_acceptance_record or allocations missing from the catalog"
        )

    perimeter = sorted(
        {
            _text(item["strategy_id"], "allocation.strategy_id")
            for item in _objects(hwm_allocations, "allocations")
            if item.get("scope") == "SYSTEMATIC" and item.get("strategy_id")
        }
    )

    try:
        validate_hwm_inputs_envelope(
            envelope,
            as_of_date=_panel_date(catalog.get("as_of_date"), "catalog.as_of_date"),
            perimeter=perimeter,
            crosswalk=_objects(catalog.get("strategy_returns", []), "strategy_returns"),
            artifact_roles=artifact_roles,
            policy_exclusions=hwm_policy_exclusions(
                _load_json(HWM_POLICY, "HWM policy")
            ),
            allocations=_objects(hwm_allocations, "allocations"),
            acceptance_record=hwm_acceptance_record,
        )
    except HwmInputsContractError as exc:
        raise ControlPanelValidationError(str(exc)) from exc


def _panel_date(value: object, field_name: str) -> date:
    accepted = _text(value, field_name)
    try:
        return date.fromisoformat(accepted)
    except ValueError as exc:
        raise ControlPanelValidationError(f"{field_name} must be an ISO date") from exc


def _source_clock_display_lines(value: object) -> list[str]:
    """Validate and render the report's replayed source clock without a wall clock."""

    if not isinstance(value, Mapping) or set(value) != {
        "latest_common_eod",
        "limiting_inputs",
        "selected_financial_cutoff",
        "source_data_as_of",
    }:
        raise ControlPanelValidationError("source clock declaration differs")
    selected = _panel_date(
        value.get("selected_financial_cutoff"), "source clock selected financial cutoff"
    ).isoformat()
    latest = _panel_date(
        value.get("latest_common_eod"), "source clock latest common EOD"
    ).isoformat()
    if latest < selected:
        raise ControlPanelValidationError(
            "source clock latest common EOD precedes cutoff"
        )
    limiting = value.get("limiting_inputs")
    rows = value.get("source_data_as_of")
    if (
        not isinstance(limiting, list)
        or not all(isinstance(item, str) and item for item in limiting)
        or limiting != sorted(set(limiting))
        or not isinstance(rows, list)
        or not rows
    ):
        raise ControlPanelValidationError("source clock provider declarations differ")
    permitted = {
        "applies_to_latest_common_eod",
        "captured_at",
        "finality",
        "native_calendar",
        "native_capture_end_utc",
        "native_capture_start_utc",
        "native_observed_at",
        "precision",
        "provider_data_date",
        "published_at",
        "source_id",
        "timezone",
    }
    source_ids: list[str] = []
    applicable_dates: dict[str, str] = {}
    rendered: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping) or set(row) - permitted:
            raise ControlPanelValidationError(
                "source clock provider declaration differs"
            )
        source_id = _text(row.get("source_id"), "source clock provider source_id")
        source_ids.append(source_id)
        applies = row.get("applies_to_latest_common_eod")
        if not isinstance(applies, bool):
            raise ControlPanelValidationError(
                "source clock provider applicability differs"
            )
        provider_date = _panel_date(
            row.get("provider_data_date"), f"source clock {source_id} data date"
        ).isoformat()
        finality = row.get("finality")
        if (
            not isinstance(finality, Mapping)
            or set(finality) != {"basis", "provider_finality_guarantee"}
            or not isinstance(finality.get("basis"), str)
            or not isinstance(finality.get("provider_finality_guarantee"), bool)
        ):
            raise ControlPanelValidationError("source clock provider finality differs")
        timezone = row.get("timezone")
        if timezone is not None and (not isinstance(timezone, str) or not timezone):
            raise ControlPanelValidationError("source clock native timezone differs")
        precision = row.get("precision")
        if precision is not None and (not isinstance(precision, str) or not precision):
            raise ControlPanelValidationError("source clock native precision differs")
        native_observed_at = row.get("native_observed_at")
        if native_observed_at is not None:
            observed_text = _text(
                native_observed_at, f"source clock {source_id} native observation"
            )
            try:
                parsed_observation = datetime.fromisoformat(observed_text)
            except ValueError as exc:
                raise ControlPanelValidationError(
                    "source clock native observation differs"
                ) from exc
            if (
                parsed_observation.tzinfo is None
                or parsed_observation.utcoffset() is None
            ):
                raise ControlPanelValidationError(
                    "source clock native observation differs"
                )
            if precision is None or precision == "DATE":
                raise ControlPanelValidationError(
                    "source clock native precision differs"
                )
        rendered_timezone = timezone if timezone is not None else "unavailable"
        rendered_precision = precision if precision is not None else "DATE"
        if applies:
            applicable_dates[source_id] = provider_date
        details = [
            f"Source clock — {source_id}: data as-of {provider_date}",
            f"finality basis {finality['basis']}",
        ]
        if native_observed_at is not None:
            details.append(
                "native observation "
                + _text(
                    native_observed_at, f"source clock {source_id} native observation"
                )
            )
        for field_name, label in (
            ("native_capture_start_utc", "native capture start"),
            ("native_capture_end_utc", "native capture end"),
            ("captured_at", "native provenance captured"),
            ("published_at", "native provenance published"),
        ):
            field_value = row.get(field_name)
            if field_value is not None:
                details.append(
                    f"{label} {_text(field_value, f'source clock {source_id} {field_name}')}"
                )
        details.append(f"native timezone {rendered_timezone}")
        details.append(f"native precision {rendered_precision}")
        rendered.append("; ".join(details) + ".")
    if source_ids != sorted(set(source_ids)) or not applicable_dates:
        raise ControlPanelValidationError("source clock provider identities differ")
    expected_latest = min(applicable_dates.values())
    expected_limiting = sorted(
        source_id
        for source_id, provider_date in applicable_dates.items()
        if provider_date == expected_latest
    )
    if latest != expected_latest or limiting != expected_limiting:
        raise ControlPanelValidationError(
            "source clock latest common EOD or limiting inputs differ"
        )
    return [
        "Source clock — selected financial cutoff "
        f"{selected}; latest common finalized EOD {latest}; limiting inputs "
        f"{', '.join(limiting)}.",
        *rendered,
        "Actual execution timestamp is not declared in this report consumer; runner receipts retain execution attempts separately.",
    ]


def _compute_panel_numerical_hwm(
    catalog: Mapping[str, Any],
    epoch_hpr_receipts: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Recompute the numerical full-systematic HWM from the governed
    baseline catalog so the panel can reconcile it against the report.

    This mirrors ``_hwm_numerical_from_envelope`` in
    ``tools/portfolio_report.py`` exactly, but reads the CATALOG (the
    hash-verified authority named by the report's baseline reference), not
    the report's carried copies: the ``hwm_inputs`` envelope is the only
    input, daily returns resolve through the catalog's hash-bound
    ``strategy_returns`` crosswalk with exactly-one identity match per
    reference (duplicates or mismatches fail closed), and absence or any
    internal defect yields UNAVAILABLE with the same structured reason —
    never an error and never a fallback.
    """

    def _unavailable(reason: str) -> dict[str, Any]:
        return {"hwm_numerical_status": "UNAVAILABLE", "hwm_numerical_reason": reason}

    hpr_receipts = _objects(
        list(epoch_hpr_receipts)
        if epoch_hpr_receipts is not None
        else catalog.get("epoch_hpr_receipts", []),
        "epoch_hpr_receipts",
    )
    if any(
        _objects(
            _object(receipt.get("receipt"), "epoch HPR receipt").get(
                "span_return_receipts"
            ),
            "epoch HPR spans",
        )
        for receipt in hpr_receipts
    ):
        return _unavailable("MISSING_DAILY_POINT_CAN_HIDE_PEAK")

    def _panel_decimal(value: object, field_name: str) -> Decimal:
        if not isinstance(value, str):
            raise ControlPanelValidationError(f"{field_name} must be decimal text")
        try:
            accepted = Decimal(value)
        except InvalidOperation as exc:
            raise ControlPanelValidationError(
                f"{field_name} must be decimal text"
            ) from exc
        if not accepted.is_finite():
            raise ControlPanelValidationError(f"{field_name} must be finite")
        return accepted

    raw = catalog.get("hwm_inputs")
    if raw is None:
        return _unavailable("NO_HWM_INPUTS")
    try:
        envelope = _object(raw, "hwm_inputs")
        periods = _objects(envelope.get("periods"), "hwm_inputs.periods")
    except ControlPanelValidationError:
        return _unavailable("HWM_INPUTS_INVALID")
    # The envelope is never trusted on its own: it must satisfy the full
    # governed contract against the catalog before any recomputation runs.
    _validate_panel_hwm_envelope(envelope, catalog)

    crosswalk: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in _objects(catalog.get("strategy_returns", []), "strategy_returns"):
        reference = row.get("account_reconciliation_sha256")
        if reference is not None:
            crosswalk[str(reference)].append(row)

    by_date: dict[str, dict[str, Mapping[str, Any]]] = defaultdict(dict)
    for row in periods:
        strategy_id = row.get("strategy_id")
        day = row.get("date")
        disposition = row.get("disposition")
        if (
            not isinstance(strategy_id, str)
            or not strategy_id
            or not isinstance(day, str)
            or not day
            or not isinstance(disposition, str)
        ):
            return _unavailable("HWM_INPUTS_INVALID")
        if strategy_id in by_date[day]:
            return _unavailable("HWM_INPUTS_INVALID")
        by_date[day][strategy_id] = row

    composite_returns: list[Decimal] = []
    with localcontext() as ctx:
        ctx.prec = 34
        ctx.rounding = ROUND_HALF_EVEN
        for day in sorted(by_date):
            members = [
                row
                for row in by_date[day].values()
                if row["disposition"] != "EXCLUDED_BY_POLICY"
            ]
            # A day is legitimately empty only when every perimeter member is
            # expected not to trade (policy-excluded members are already
            # removed from `members`).  A required day whose members are all
            # UNAVAILABLE or NOT_ADMITTED is a coverage gap, never a
            # skippable period.
            if all(row["disposition"] == "EXPECTED_NON_TRADING" for row in members):
                continue
            if any(row["disposition"] != "AVAILABLE" for row in members):
                return _unavailable("INCOMPLETE_PERIODS")
            weighted_return = Decimal(0)
            total_capital = Decimal(0)
            for member in sorted(members, key=lambda item: str(item["strategy_id"])):
                try:
                    capital = _panel_decimal(
                        member.get("beginning_capital_usd"),
                        "hwm_inputs.beginning_capital_usd",
                    )
                except ControlPanelValidationError:
                    return _unavailable("HWM_INPUTS_INVALID")
                reference = member.get("return_source_record_id")
                if not isinstance(reference, str):
                    return _unavailable("UNRESOLVED_RETURN_REFERENCE")
                candidates = crosswalk.get(reference, [])
                if len(candidates) != 1:
                    # Zero rows: unresolved; more than one row carrying the
                    # reference: a duplicate must never overwrite the proven
                    # row — both fail closed.
                    return _unavailable(
                        "DUPLICATE_RETURN_REFERENCE"
                        if candidates
                        else "UNRESOLVED_RETURN_REFERENCE"
                    )
                match = candidates[0]
                if (
                    str(match.get("strategy_id") or "") != str(member["strategy_id"])
                    or str(match.get("date") or "") != day
                    or match.get("evidence_layer") != "ACCOUNT_LIVE"
                ):
                    return _unavailable("UNRESOLVED_RETURN_REFERENCE")
                weighted_return += capital * _panel_decimal(
                    match.get("return"), "strategy_returns.return"
                )
                total_capital += capital
            if total_capital <= 0:
                return _unavailable("INCOMPLETE_PERIODS")
            composite_returns.append(weighted_return / total_capital)

    if not composite_returns:
        return _unavailable("NO_AVAILABLE_PERIODS")
    result = compute_full_systematic_hwm_index(
        composite_returns,
        full_systematic_coverage=True,
    )
    if result.get("status") != "AVAILABLE":
        return _unavailable("INCOMPLETE_PERIODS")
    return {
        "hwm_index": decimal_text(result["index_series"][-1]),
        "hwm_current_drawdown": decimal_text(result["current_drawdown"]),
        "hwm_maximum_drawdown": decimal_text(result["maximum_drawdown"]),
        "hwm_numerical_status": "AVAILABLE",
    }


def _panel_portable_hwm_proof(
    receipt: Mapping[str, Any],
    input_generation_bytes: bytes,
    calendar_bytes: bytes,
    *,
    policy_bytes: bytes | None = None,
    product_contract_bytes: bytes | None = None,
    performance_policy_bytes: bytes | None = None,
    legacy_policy_bytes: bytes | None = None,
) -> dict[str, Any]:
    """Independently project a verified portable proof for panel reconciliation."""

    try:
        accepted = validate_as_held_hwm_receipt_generation_binding(
            receipt,
            input_generation_bytes,
            policy_bytes=(
                policy_bytes
                if policy_bytes is not None
                else _PORTABLE_HWM_POLICY.read_bytes()
            ),
            product_contract_bytes=(
                product_contract_bytes
                if product_contract_bytes is not None
                else _PORTABLE_HWM_PRODUCT_CONTRACT.read_bytes()
            ),
            performance_policy_bytes=(
                performance_policy_bytes
                if performance_policy_bytes is not None
                else _PORTABLE_HWM_PERFORMANCE_POLICY.read_bytes()
            ),
            legacy_policy_bytes=(
                legacy_policy_bytes
                if legacy_policy_bytes is not None
                else HWM_POLICY.read_bytes()
            ),
            calendar_bytes=calendar_bytes,
        )
    except HwmCoverageError as exc:
        raise ControlPanelValidationError(
            f"portable HWM proof is invalid: {exc}"
        ) from exc
    return {
        "schema_version": 1,
        "status": accepted["status"],
        "financial_acceptance": "NOT_FINANCIAL_ACCEPTANCE",
        "assurance_profile": accepted["assurance_profile"],
        "policy_id": accepted["policy_id"],
        "policy_sha256": accepted["policy_sha256"],
        "policy_semantic_digest": accepted["policy_semantic_digest"],
        "receipt_sha256": accepted["receipt_sha256"],
        "input_authority_class": accepted["input_authority_class"],
        "calendar_authority_status": accepted["calendar_authority_status"],
        "input_generation_sha256": accepted["input_generation_sha256"],
        "input_sha256": accepted["input_sha256"],
        "return_basis": accepted["return_basis"],
        "evaluated_on": accepted["evaluated_on"],
        "complete_through": accepted["complete_through"],
        "calculation_age_days": accepted["calculation_age_days"],
        "maximum_age_days": accepted["maximum_age_days"],
        "age_status": accepted["age_status"],
        "warnings": accepted["warnings"],
        "current_index": accepted["current_index"],
        "current_hwm": accepted["current_hwm"],
        "current_drawdown": accepted["current_drawdown"],
        "maximum_drawdown": accepted["maximum_drawdown"],
        "index_path": accepted["index_path"],
    }


def _panel_governed_hwm_proof(
    result_receipt: Mapping[str, Any],
    input_generation_bytes: bytes,
    calendar_bytes: bytes,
    input_receipt_bytes: bytes,
    *,
    policy_bytes: bytes | None = None,
    product_contract_bytes: bytes | None = None,
    performance_policy_bytes: bytes | None = None,
    legacy_policy_bytes: bytes | None = None,
) -> dict[str, Any]:
    """Independently verify + project a governed result for panel reconciliation.

    Delegates to the kernel's own verifier — the panel never reimplements
    governed arithmetic (same discipline as ``_panel_portable_hwm_proof``).
    """
    try:
        accepted = verify_governed_hwm_result(
            result_receipt,
            input_generation_bytes,
            calendar_bytes=calendar_bytes,
            input_receipt_bytes=input_receipt_bytes,
            policy_bytes=(
                policy_bytes
                if policy_bytes is not None
                else _PORTABLE_HWM_POLICY.read_bytes()
            ),
            product_contract_bytes=(
                product_contract_bytes
                if product_contract_bytes is not None
                else _PORTABLE_HWM_PRODUCT_CONTRACT.read_bytes()
            ),
            performance_policy_bytes=(
                performance_policy_bytes
                if performance_policy_bytes is not None
                else _PORTABLE_HWM_PERFORMANCE_POLICY.read_bytes()
            ),
            legacy_policy_bytes=(
                legacy_policy_bytes
                if legacy_policy_bytes is not None
                else HWM_POLICY.read_bytes()
            ),
        )
    except HwmCoverageError as exc:
        raise ControlPanelValidationError(
            f"governed HWM result is invalid: {exc}"
        ) from exc
    return {
        "schema_version": 1,
        "status": accepted["status"],
        "financial_acceptance": "PENDING_OWNER_ACCEPTANCE",
        "assurance_profile": accepted["assurance_profile"],
        "policy_id": accepted["policy_id"],
        "policy_sha256": accepted["policy_sha256"],
        "calendar_sha256": accepted["calendar_sha256"],
        "input_generation_sha256": accepted["input_generation_sha256"],
        "upstream_receipt_sha256": accepted["upstream_receipt_sha256"],
        "receipt_sha256": accepted["receipt_sha256"],
        "input_authority_class": accepted["input_authority_class"],
        "return_basis": accepted["return_basis"],
        "evaluated_on": accepted["evaluated_on"],
        "complete_through": accepted["complete_through"],
        "calculation_age_days": accepted["calculation_age_days"],
        "maximum_age_days": accepted["maximum_age_days"],
        "age_status": accepted["age_status"],
        "warnings": accepted["warnings"],
        # Canonical decimal TEXT so the panel's JSON serialization round-trips.
        "current_index": decimal_text(accepted["current_index"]),
        "current_hwm": decimal_text(accepted["current_hwm"]),
        "current_drawdown": decimal_text(accepted["current_drawdown"]),
        "maximum_drawdown": decimal_text(accepted["maximum_drawdown"]),
        "index_path": [
            {
                "as_of": row["as_of"],
                "index": decimal_text(row["index"]),
                "hwm": decimal_text(row["hwm"]),
                "drawdown": decimal_text(row["drawdown"]),
            }
            for row in accepted["index_path"]
        ],
    }


def _reconcile_hwm_coverage(
    coverage: Sequence[Mapping[str, Any]], hwm_summary: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Make legacy report coverage agree with the governed HWM perimeter."""

    normalized = [dict(row) for row in coverage]
    missing = [str(name) for name in hwm_summary["uncovered_strategies"]]
    details = [
        dict(item)
        for item in hwm_summary.get("uncovered_details", [])
        if isinstance(item, Mapping)
    ]
    detail_by_name = {str(item.get("strategy_name")): item for item in details}
    gap_details = ", ".join(
        f"{name} (${detail_by_name.get(name, {}).get('current_value_usd', 'UNKNOWN')}; "
        f"{detail_by_name.get(name, {}).get('qualification_gap', 'UNQUALIFIED')}; "
        f"last accepted {detail_by_name.get(name, {}).get('last_accepted_date') or 'unavailable'}; "
        f"{detail_by_name.get(name, {}).get('freshness_reason') or 'no freshness reason'})"
        for name in missing
    )
    excluded = [
        dict(item)
        for item in hwm_summary.get("excluded_details", [])
        if isinstance(item, Mapping)
    ]
    exclusion_text = (
        "explicit policy exclusions: "
        + ", ".join(
            f"{item.get('strategy_name')} (${item.get('current_value_usd')}; {item.get('reason')})"
            for item in excluded
        )
        if excluded
        else (
            f"{len(hwm_summary['excluded_strategies'])} strateg"
            f"{'y is' if len(hwm_summary['excluded_strategies']) == 1 else 'ies are'} "
            "explicitly excluded de minimis by policy"
        )
    )
    for row in normalized:
        if row.get("category") == "FACT_COVERAGE" and row.get("item") == "nav":
            row["known_gap"] = (
                (
                    f"{len(missing)} included HWM-perimeter strateg"
                    f"{'y is' if len(missing) == 1 else 'ies are'} not HWM-qualified"
                    + (f": {gap_details}" if gap_details else "")
                )
                if missing
                else "Every included HWM-perimeter strategy is qualified"
            )
            row["known_gap"] += f"; {exclusion_text}"
            row["disposition"] = (
                "RETAINED_UNCOVERED_PENDING_FRESH_DAILY_NAV" if missing else "COMPLETE"
            )
            row["status"] = (
                "PARTIAL" if missing else "COMPLETE_WITHIN_DECLARED_BOUNDARY"
            )

    hwm_rows = [
        row
        for row in normalized
        if row.get("category") == "RECONCILIATION_EXCEPTION"
        and row.get("item") == "full-systematic-hwm-unavailable"
    ]
    if not missing:
        return [row for row in normalized if row not in hwm_rows]

    gap = (
        "BLOCKING_FOR_AUTOMATED_HWM: included HWM-perimeter strategies are not "
        f"qualified; exact uncovered value ${hwm_summary.get('uncovered_value_usd', 'UNKNOWN')}: "
        + gap_details
    )
    if hwm_rows:
        for row in hwm_rows:
            row["known_gap"] = gap
            row["disposition"] = "RETAINED_UNCOVERED_PENDING_FRESH_DAILY_NAV"
            row["resolution_authority"] = (
                "PORT-HWM-3 policy and admitted-lane daily NAV freshness evidence"
            )
            row["status"] = "UNAVAILABLE"
            row["uncovered_details"] = details
            # The details carry the current cutoff weights of the strategy rows.
            row["weight_basis"] = WEIGHT_BASIS_CURRENT
            row["value"] = hwm_summary.get("uncovered_value_usd")
    else:
        normalized.append(
            {
                "category": "RECONCILIATION_EXCEPTION",
                "item": "full-systematic-hwm-unavailable",
                "known_gap": gap,
                "disposition": "RETAINED_UNCOVERED_PENDING_FRESH_DAILY_NAV",
                "resolution_authority": (
                    "PORT-HWM-3 policy and admitted-lane daily NAV freshness evidence"
                ),
                "status": "UNAVAILABLE",
                "uncovered_details": details,
                "value": hwm_summary.get("uncovered_value_usd"),
                "weight_basis": WEIGHT_BASIS_CURRENT,
            }
        )
    return normalized


def _factor_contribution_summary(model: Mapping[str, Any]) -> dict[str, Any]:
    """Project the producer's reconciled holdout contribution rows."""
    contributions = _objects(model.get("contributions"), "model.contributions")
    if not contributions:
        raise ControlPanelValidationError("selected factor model has no contributions")
    factor_ids = tuple(
        _text(item, "model.factor_ids[]") for item in model.get("factor_ids", [])
    )
    if not factor_ids:
        raise ControlPanelValidationError("selected factor model has no factors")
    maximum_error = 0.0
    test_rows: list[dict[str, Any]] = []
    for contribution in contributions:
        factor_values = _object(
            contribution.get("factor_contributions"),
            "contribution.factor_contributions",
        )
        if set(factor_values) != set(factor_ids):
            raise ControlPanelValidationError(
                "factor contribution columns differ from selected factors"
            )
        factor_total = sum(
            _finite_float(value, f"contribution.{factor_id}")
            for factor_id, value in factor_values.items()
        )
        alpha_value = _finite_float(
            contribution.get("alpha_contribution"),
            "contribution.alpha_contribution",
        )
        fitted = _finite_float(
            contribution.get("fitted_excess_return"),
            "contribution.fitted_excess_return",
        )
        actual = _finite_float(
            contribution.get("actual_excess_return"),
            "contribution.actual_excess_return",
        )
        residual = _finite_float(contribution.get("residual"), "contribution.residual")
        maximum_error = max(
            maximum_error,
            abs(fitted - alpha_value - factor_total),
            abs(actual - fitted - residual),
        )
        if contribution.get("partition") == "TEST":
            test_rows.append(contribution)
    if maximum_error > 1e-9 or not test_rows:
        raise ControlPanelValidationError(
            "factor contributions do not reconcile or lack holdout evidence"
        )

    def mean(field: str) -> str:
        return _number_text(
            sum(
                _finite_float(row.get(field), f"contribution.{field}")
                for row in test_rows
            )
            / len(test_rows)
        )

    factor_means = {
        factor_id: _number_text(
            sum(
                _finite_float(
                    _object(
                        row.get("factor_contributions"),
                        "contribution.factor_contributions",
                    ).get(factor_id),
                    f"contribution.{factor_id}",
                )
                for row in test_rows
            )
            / len(test_rows)
        )
        for factor_id in factor_ids
    }
    return {
        "alpha_mean": mean("alpha_contribution"),
        "factor_means": factor_means,
        "fitted_mean": mean("fitted_excess_return"),
        "maximum_reconciliation_error": _number_text(maximum_error),
        "observations": len(test_rows),
        "realized_mean": mean("actual_excess_return"),
        "residual_mean": mean("residual"),
    }


def _schema_three_orthogonal_surface(
    model: Mapping[str, Any], model_id: str, methodology_manifest_sha256: str
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Accept only the producer's named raw and orthogonal analytics receipts."""
    orthogonal = _object(
        model.get("orthogonal_attribution"), "model.orthogonal_attribution"
    )
    identities = _object(
        model.get("analytics_identities"), "model.analytics_identities"
    )
    raw_identity = _object(identities.get("raw"), "model.analytics_identities.raw")
    orthogonal_identity = _object(
        identities.get("orthogonal"), "model.analytics_identities.orthogonal"
    )
    if (
        raw_identity.get("model_id") != model_id
        or raw_identity.get("methodology_manifest_sha256")
        != methodology_manifest_sha256
        or raw_identity.get("surface") not in {"RAW_MODEL", "RAW_CONSTRAINED_RBSA"}
        or orthogonal_identity.get("model_id") != model_id
        or orthogonal_identity.get("methodology_manifest_sha256")
        != methodology_manifest_sha256
        or orthogonal_identity.get("surface") != "ORTHOGONAL_RISK_ATTRIBUTION"
    ):
        raise ControlPanelValidationError("schema-three orthogonal identity differs")
    interval = _object(model.get("interval_association"), "model.interval_association")
    if (
        interval.get("evaluation_information_set") != "RETROSPECTIVE_SELECTED_SNAPSHOT"
        or interval.get("holdout_display_claim")
        != "holdout association using selected historical snapshot"
    ):
        raise ControlPanelValidationError("schema-three orthogonal interval differs")
    return orthogonal, identities, interval


def _factor_analysis(
    decomposition: Mapping[str, Any],
    identity: Mapping[str, str],
    strategy_names: set[str],
) -> dict[str, Any]:
    analytics_schema_version = decomposition.get("analytics_schema_version")
    if analytics_schema_version not in {1, 2, 3}:
        raise ControlPanelValidationError("factor analytics schema is unsupported")
    method_label = _text(decomposition.get("method_label"), "factor.method_label")
    methodology_version = _text(
        decomposition.get("methodology_version"), "factor.methodology_version"
    )
    evidence_mode = _text(
        decomposition.get("evidence_mode", "LIVE_ONLY"),
        "factor.evidence_mode",
    )
    raw_composition = decomposition.get("series_composition", {})
    composition = _object(raw_composition, "factor.series_composition")
    series_composition: dict[str, dict[str, Any]] = {}
    for declared_series_id, raw_value in composition.items():
        if not isinstance(declared_series_id, str) or not declared_series_id:
            raise ControlPanelValidationError(
                "factor series composition has an invalid series id"
            )
        value = _object(raw_value, f"factor.series_composition.{declared_series_id}")
        if value.get("series_id", declared_series_id) != declared_series_id:
            raise ControlPanelValidationError(
                f"factor series composition identity differs: {declared_series_id}"
            )
        if value.get("evidence_mode", evidence_mode) != evidence_mode:
            raise ControlPanelValidationError(
                f"factor series composition mode differs: {declared_series_id}"
            )
        series_composition[declared_series_id] = value
    selections = _objects(decomposition.get("model_selections"), "model_selections")
    if {
        _text(row.get("series_id"), "selection.series_id") for row in selections
    } != strategy_names:
        raise ControlPanelValidationError(
            "factor selections differ from the current strategy boundary"
        )
    models = _objects(decomposition.get("model_results"), "model_results")
    model_index = {
        (
            _text(row.get("series_id"), "model.series_id"),
            _text(row.get("model_id"), "model.model_id"),
        ): row
        for row in models
    }
    attempts: dict[str, list[dict[str, Any]]] = {}
    for row in models:
        attempts.setdefault(_text(row.get("series_id"), "model.series_id"), []).append(
            row
        )

    rows: list[dict[str, Any]] = []
    available_count = 0
    diagnostic_count = 0
    dynamic_available_count = 0
    for selection in sorted(selections, key=lambda row: str(row.get("series_id"))):
        series_id = _text(selection.get("series_id"), "selection.series_id")
        selected_model = selection.get("model_id")
        if selected_model is None:
            series_attempts = attempts.get(series_id, [])
            reasons = sorted(
                {_text(item.get("status"), "model.status") for item in series_attempts}
            )
            best_coverage: dict[str, Any] | None = None
            if series_attempts:
                best = max(
                    series_attempts,
                    key=lambda item: int(
                        _object(item.get("coverage"), "model.coverage").get(
                            "aligned_observations", 0
                        )
                    ),
                )
                best_coverage = _object(best.get("coverage"), "model.coverage")
            rows.append(
                {
                    "alpha": None,
                    "contribution_summary": None,
                    "cost_coverage": "INCOMPLETE",
                    "coverage": best_coverage,
                    "diagnostics": None,
                    "estimator": None,
                    "evidence_layer": None,
                    "exposures": [],
                    "model_id": None,
                    "model_quality": {
                        "decision_rule": "SEALED_HOLDOUT_PREDICTED_R2_GT_ZERO",
                        "decision_status": "UNAVAILABLE",
                        "gate_reason": (
                            reasons[0] if reasons else "UNAVAILABLE_NO_MODEL_EVIDENCE"
                        ),
                        "holdout_predicted_r_squared": None,
                    },
                    "selection_status": _text(
                        selection.get("status"), "selection.status"
                    ),
                    "series_composition": series_composition.get(series_id, {}),
                    "series_id": series_id,
                    "status": "UNAVAILABLE",
                    "unavailable_reasons": reasons or ["UNAVAILABLE_NO_MODEL_EVIDENCE"],
                }
            )
            continue

        model_id = _text(selected_model, "selection.model_id")
        model = model_index.get((series_id, model_id))
        if model is None:
            raise ControlPanelValidationError(
                f"selected factor model is absent: {series_id}"
            )
        estimator = _text(model.get("estimator", "STATIC_RBSA"), "model.estimator")
        computational_status = _text(
            model.get("computational_status"), "model.computational_status"
        )
        model_quality = _object(model.get("model_quality"), "model.model_quality")
        diagnostics = _object(model.get("diagnostics"), "model.diagnostics")
        holdout = _object(diagnostics.get("holdout"), "model.diagnostics.holdout")
        predicted_r_squared = holdout.get("predicted_r_squared")
        predicted_value = (
            None
            if predicted_r_squared is None
            else _finite_float(predicted_r_squared, "holdout.predicted_r_squared")
        )
        decision_status = _text(
            model_quality.get("decision_status"), "model_quality.decision_status"
        )
        model_status = _text(model.get("status"), "model.status")
        orthogonal_attribution: dict[str, Any] | None = None
        analytics_identities: dict[str, Any] | None = None
        interval_association: dict[str, Any] | None = None
        if analytics_schema_version == 3:
            (
                orthogonal_attribution,
                analytics_identities,
                interval_association,
            ) = _schema_three_orthogonal_surface(
                model,
                model_id,
                _text(
                    decomposition.get("methodology_manifest_sha256"),
                    "factor.methodology_manifest_sha256",
                ),
            )
        orthogonal_surface_fields = (
            {
                "analytics_identities": analytics_identities,
                "interval_association": interval_association,
                "orthogonal_attribution": orthogonal_attribution,
            }
            if analytics_schema_version == 3
            else {}
        )
        if computational_status != "AVAILABLE":
            raise ControlPanelValidationError(
                f"selected factor model was not computed: {series_id}"
            )
        if model_status == "DIAGNOSTIC_ONLY":
            if decision_status != "DIAGNOSTIC_ONLY" or (
                predicted_value is not None and predicted_value > 0
            ):
                raise ControlPanelValidationError(
                    f"diagnostic model quality gate is inconsistent: {series_id}"
                )
            if selection.get("status") != "SELECTED_DIAGNOSTIC_ONLY_TRAIN_CV_MODEL":
                raise ControlPanelValidationError(
                    f"diagnostic selection state is inconsistent: {series_id}"
                )
            raw_diagnostic_outputs = model.get("diagnostic_outputs")
            diagnostic_outputs = (
                raw_diagnostic_outputs
                if isinstance(raw_diagnostic_outputs, dict)
                else {}
            )
            diagnostic_alpha = (
                _object(
                    diagnostic_outputs.get("alpha"),
                    "model.diagnostic_outputs.alpha",
                )
                if diagnostic_outputs.get("alpha") is not None
                else None
            )
            diagnostic_exposures = (
                _objects(
                    diagnostic_outputs.get("exposures"),
                    "model.diagnostic_outputs.exposures",
                )
                if diagnostic_outputs.get("exposures") is not None
                else []
            )
            rows.append(
                {
                    "alpha": diagnostic_alpha,
                    "contribution_summary": (
                        _factor_contribution_summary(model)
                        if model.get("contributions")
                        else None
                    ),
                    "cost_coverage": _text(
                        model.get("cost_coverage"), "model.cost_coverage"
                    ),
                    "coverage": _object(model.get("coverage"), "model.coverage"),
                    "diagnostics": diagnostics,
                    "estimator": estimator,
                    "evidence_layer": _text(
                        model.get("evidence_layer"), "model.evidence_layer"
                    ),
                    "exposures": diagnostic_exposures,
                    "model_id": model_id,
                    "model_quality": model_quality,
                    **orthogonal_surface_fields,
                    "selection_status": _text(
                        selection.get("status"), "selection.status"
                    ),
                    "series_composition": series_composition.get(series_id, {}),
                    "series_id": series_id,
                    "status": "DIAGNOSTIC_ONLY",
                    "use_boundary": "FORENSIC_DIAGNOSTIC_NOT_PM_ACCEPTED",
                    "unavailable_reasons": [
                        _text(
                            model_quality.get("gate_reason"),
                            "model_quality.gate_reason",
                        )
                    ],
                }
            )
            diagnostic_count += 1
            continue
        if (
            model_status != "AVAILABLE"
            or decision_status != "PM_VALID"
            or predicted_value is None
            or predicted_value <= 0
        ):
            raise ControlPanelValidationError(
                f"selected factor model is not PM-valid: {series_id}"
            )
        rows.append(
            {
                "alpha": _object(model.get("alpha"), "model.alpha"),
                "contribution_summary": _factor_contribution_summary(model),
                "cost_coverage": _text(
                    model.get("cost_coverage"), "model.cost_coverage"
                ),
                "coverage": _object(model.get("coverage"), "model.coverage"),
                "diagnostics": diagnostics,
                "estimator": estimator,
                "evidence_layer": _text(
                    model.get("evidence_layer"), "model.evidence_layer"
                ),
                "exposures": _objects(model.get("exposures"), "model.exposures"),
                "model_id": model_id,
                "model_quality": model_quality,
                **orthogonal_surface_fields,
                "selection_status": _text(selection.get("status"), "selection.status"),
                "series_composition": series_composition.get(series_id, {}),
                "series_id": series_id,
                "status": "AVAILABLE",
                "unavailable_reasons": [],
            }
        )
        available_count += 1
        dynamic_available_count += estimator == "DYNAMIC_FLS"
    proxy_governance = decomposition.get("proxy_governance")
    if proxy_governance is None:
        accepted_proxy_governance: dict[str, Any] = {
            "candidate_count": 0,
            "orthogonality_claim": "UNAVAILABLE_LEGACY_ANALYTICS_SCHEMA",
            "proxies": [],
            "selection_boundary": "UNAVAILABLE_LEGACY_ANALYTICS_SCHEMA",
        }
    else:
        accepted_proxy_governance = _object(proxy_governance, "factor.proxy_governance")
        proxies = _objects(
            accepted_proxy_governance.get("proxies"),
            "factor.proxy_governance.proxies",
        )
        if accepted_proxy_governance.get("candidate_count") != len(proxies):
            raise ControlPanelValidationError("factor proxy candidate count differs")
    if analytics_schema_version in {2, 3} and not accepted_proxy_governance["proxies"]:
        raise ControlPanelValidationError("schema-v2 factor proxy governance is absent")
    return {
        "analytics_schema_version": analytics_schema_version,
        "analytics_hash": _text(
            decomposition.get("analytics_hash"), "factor.analytics_hash"
        ),
        "as_of_date": _text(decomposition.get("as_of_date"), "factor.as_of_date"),
        "available_count": available_count,
        "diagnostic_count": diagnostic_count,
        "dynamic_available_count": dynamic_available_count,
        "evidence_mode": evidence_mode,
        "generation_id": identity["generation_id"],
        "method_label": method_label,
        "methodology_manifest_sha256": _text(
            decomposition.get("methodology_manifest_sha256"),
            "factor.methodology_manifest_sha256",
        ),
        "methodology_version": methodology_version,
        "proxy_governance": accepted_proxy_governance,
        "series_composition": series_composition,
        "series": rows,
        "source_sha256": identity["sha256"],
        "sources": _object(decomposition.get("sources"), "factor.sources"),
        "unavailable_count": sum(row["status"] == "UNAVAILABLE" for row in rows),
    }


def _unavailable_factor_row(series_id: str, reason: str) -> dict[str, Any]:
    return {
        "alpha": None,
        "contribution_summary": None,
        "cost_coverage": "INCOMPLETE",
        "coverage": None,
        "diagnostics": None,
        "estimator": None,
        "evidence_layer": None,
        "exposures": [],
        "model_id": None,
        "model_quality": {
            "decision_rule": "SEALED_HOLDOUT_PREDICTED_R2_GT_ZERO",
            "decision_status": "UNAVAILABLE",
            "gate_reason": reason,
            "holdout_predicted_r_squared": None,
        },
        "selection_status": reason,
        "series_id": series_id,
        "source_key": None,
        "source_label": None,
        "status": "UNAVAILABLE",
        "unavailable_reasons": [reason],
    }


def _dynamic_factor_lens(
    decomposition: Mapping[str, Any],
    identity: Mapping[str, str],
    strategy_names: set[str],
) -> dict[str, Any]:
    if decomposition.get("analysis_lens") not in {
        None,
        "HISTORICAL_MINIMUM_PROOF",
    }:
        raise ControlPanelValidationError("dynamic decomposition lens differs")
    return {
        **_factor_analysis(decomposition, identity, strategy_names),
        "as_of_policy": "SINGLE_ACCEPTED_GENERATION",
        "evidence_classification": "MPI_INFORMED_NON_EQUIVALENT",
        "lens_id": "DYNAMIC_STYLE",
        "lens_label": "Dynamic style path (clean-room FLS)",
        "release_scope": "SEALED_HOLDOUT_GATED_DYNAMIC_ANALYSIS",
        "return_basis": "CONFIGURED_EXCESS_RETURN",
    }


def _factor_decomposition_perimeter(
    decomposition: Mapping[str, Any], required_names: set[str]
) -> tuple[dict[str, Any], set[str]]:
    """Select one governed analytics perimeter without mutating its source."""

    selections = _objects(decomposition.get("model_selections"), "model_selections")
    selected_names = {
        _text(row.get("series_id"), "selection.series_id") for row in selections
    } & required_names
    models = _objects(decomposition.get("model_results"), "model_results")
    composition = _object(
        decomposition.get("series_composition", {}), "factor.series_composition"
    )
    return (
        {
            **decomposition,
            "model_selections": [
                row for row in selections if row.get("series_id") in selected_names
            ],
            "model_results": [
                row for row in models if row.get("series_id") in selected_names
            ],
            "series_composition": {
                key: value
                for key, value in composition.items()
                if key in selected_names
            },
        },
        selected_names,
    )


def _current_primary_factor_analyses(
    decomposition: Mapping[str, Any],
    identity: Mapping[str, str],
    strategy_names: Mapping[str, str],
    contract_snapshot: bytes | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    required_names = _dependence_strategy_names(strategy_names, contract_snapshot)
    filtered, selected_names = _factor_decomposition_perimeter(
        decomposition, required_names
    )
    if selected_names != required_names:
        raise ControlPanelValidationError(
            "primary factor series differ from the governed product perimeter"
        )
    return (
        _factor_analysis(filtered, identity, required_names),
        _dynamic_factor_lens(filtered, identity, required_names),
    )


def verify_decomposition_surface_against_plan(
    plan_bundle: Path,
    source_key: str,
    surface: str,
    evidence_mode: str,
    decomposition: Mapping[str, Any],
    generation_manifest_path: Path,
) -> dict[str, Any]:
    """Bind one accepted decomposition generation to its exact planned surface."""

    if surface not in {"STATIC", "DYNAMIC_FLS"}:
        raise ControlPanelValidationError("decomposition surface is unsupported")
    manifest, _content, _sha256 = _json_snapshot(
        plan_bundle / "plan-manifest.json", "decomposition plan manifest"
    )
    if (
        manifest.get("builder") != "portfolio-current-decomposition-plan"
        or (manifest.get("builder_version"), manifest.get("schema_version"))
        not in {("0.17.0", 2), ("0.18.0", 2)}
        or manifest.get("bundle_id") != plan_bundle.name
    ):
        raise ControlPanelValidationError("current decomposition plan identity differs")
    spec_rows = [
        row
        for row in _objects(manifest.get("specs"), "decomposition plan specs")
        if row.get("source_key") == source_key and row.get("surface") == surface
    ]
    if len(spec_rows) != 1:
        raise ControlPanelValidationError(
            f"decomposition plan requires one {source_key}:{surface} spec"
        )
    spec_row = spec_rows[0]
    expected_estimator = "STATIC_RBSA" if surface == "STATIC" else "DYNAMIC_FLS"
    expected_spec_id = f"{source_key}:{surface}"
    if spec_row.get("spec_id") != expected_spec_id:
        raise ControlPanelValidationError("decomposition plan spec identity differs")
    relative = _text(spec_row.get("path"), "decomposition plan spec path")
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ControlPanelValidationError("decomposition plan spec path is unsafe")
    spec_path = plan_bundle / relative_path
    spec, spec_content, spec_sha256 = _json_snapshot(
        spec_path, f"{source_key} {surface} decomposition plan spec"
    )
    expected_sha256 = _text(spec_row.get("sha256"), "decomposition plan spec sha256")
    expected_size = spec_row.get("size_bytes")
    if not isinstance(expected_size, int) or expected_size <= 0:
        raise ControlPanelValidationError("decomposition plan spec size is invalid")
    manifest_outputs = {
        _text(row.get("path"), "decomposition plan output path"): row
        for row in _objects(manifest.get("outputs"), "decomposition plan outputs")
    }
    declared_output = manifest_outputs.get(relative)
    if (
        spec_sha256 != expected_sha256
        or len(spec_content) != expected_size
        or declared_output is None
        or declared_output.get("sha256") != expected_sha256
        or declared_output.get("size_bytes") != expected_size
    ):
        raise ControlPanelValidationError("decomposition plan spec bytes differ")
    if (
        spec.get("schema_version") != 1
        or spec.get("analysis_lens") != spec_row.get("analysis_lens")
        or spec.get("analysis_lens") != decomposition.get("analysis_lens")
        or spec.get("evidence_mode") != evidence_mode
        or decomposition.get("evidence_mode", "LIVE_ONLY") != evidence_mode
    ):
        raise ControlPanelValidationError("decomposition plan spec boundary differs")
    spec_models = _objects(spec.get("models"), "decomposition plan spec models")
    model_ids = [
        _text(row.get("model_id"), "decomposition spec model_id") for row in spec_models
    ]
    if (
        not model_ids
        or len(model_ids) != len(set(model_ids))
        or spec_row.get("model_count") != len(model_ids)
        or {
            _text(row.get("estimator"), "decomposition spec estimator")
            for row in spec_models
        }
        != {expected_estimator}
    ):
        raise ControlPanelValidationError("decomposition plan surface models differ")
    result_models = _objects(
        decomposition.get("model_results"), "decomposition surface model_results"
    )
    result_model_ids = {
        _text(row.get("model_id"), "decomposition result model_id")
        for row in result_models
    }
    selected_model_ids = {
        _text(row.get("model_id"), "decomposition selection model_id")
        for row in _objects(
            decomposition.get("model_selections"),
            "decomposition surface model_selections",
        )
        if row.get("model_id") is not None
    }
    if (
        result_model_ids != set(model_ids)
        or not selected_model_ids <= result_model_ids
        or {
            _text(row.get("estimator", "STATIC_RBSA"), "decomposition estimator")
            for row in result_models
        }
        != {expected_estimator}
    ):
        raise ControlPanelValidationError("decomposition result surface models differ")
    try:
        generation_manifest_content, _generation_sha256 = _read_snapshot(
            generation_manifest_path, "decomposition generation manifest"
        )
        generation_manifest = GenerationManifest.from_json_bytes(
            generation_manifest_content
        )
    except (ManifestValidationError, OSError, ValueError, RuntimeError) as exc:
        raise ControlPanelValidationError(
            "decomposition generation manifest is invalid"
        ) from exc
    bound_spec = next(
        (
            artifact
            for artifact in generation_manifest.artifacts
            if artifact.artifact_id == "spec/decomposition-spec.json"
        ),
        None,
    )
    if bound_spec is None or (
        bound_spec.sha256 != spec_sha256 or bound_spec.size_bytes != len(spec_content)
    ):
        raise ControlPanelValidationError(
            "decomposition generation is not bound to the planned surface spec"
        )
    return {
        "evidence_mode": evidence_mode,
        "model_count": len(model_ids),
        "sha256": spec_sha256,
        "size_bytes": len(spec_content),
        "source_key": source_key,
        "spec_id": expected_spec_id,
        # bundle-relative: publish snapshots the plan bundle to a temporary root
        # and rebuilds the report, so an absolute path here would make the report
        # identity depend on where the bundle happens to be staged
        "spec_path": relative_path.as_posix(),
        "surface": surface,
    }


def _group_decomposition_lens_sources(
    *,
    lens_id: str,
    label: str,
    source_keys: Sequence[str],
    return_basis: str,
    source_analyses: Mapping[str, Mapping[str, Any]],
    analysis_strategy_names: set[str],
    series_to_reason: Mapping[str, str],
    disposition_reasons: Mapping[str, Mapping[str, str]],
    current_plan: bool,
    plan_bundle_id: str,
    evidence_mode: str,
    surface: str,
) -> dict[str, Any]:
    analyses = [source_analyses[source_key] for source_key in source_keys]
    rows: list[dict[str, Any]] = []
    if current_plan:
        # A current product cell is source-qualified.  The same strategy may be
        # selected in both regional economic sources or both style sources;
        # preserve each result instead of collapsing by series_id.
        for source_key, analysis in zip(source_keys, analyses, strict=True):
            source_label = _text(analysis.get("source_label"), "source label")
            rows_by_series = {
                _text(row.get("series_id"), "lens series_id"): dict(row)
                for row in _objects(analysis.get("series"), "decomposition lens series")
            }
            if len(rows_by_series) != len(
                _objects(analysis.get("series"), "decomposition lens series")
            ):
                raise ControlPanelValidationError(
                    f"decomposition source duplicates a series: {source_key}"
                )
            for series_id in sorted(analysis_strategy_names):
                row = rows_by_series.get(series_id)
                if row is None:
                    row = _unavailable_factor_row(
                        series_id,
                        disposition_reasons.get(series_id, {}).get(
                            source_key, series_to_reason[series_id]
                        ),
                    )
                row.update(
                    {
                        "source_key": source_key,
                        "source_label": source_label,
                        "surface": surface,
                    }
                )
                rows.append(row)
    else:
        combined_rows_by_series: dict[str, dict[str, Any]] = {}
        for analysis in analyses:
            for row in _objects(analysis.get("series"), "decomposition lens series"):
                series_id = _text(row.get("series_id"), "lens series_id")
                if series_id in combined_rows_by_series:
                    raise ControlPanelValidationError(
                        f"decomposition lens duplicates {series_id}: {lens_id}"
                    )
                combined_rows_by_series[series_id] = dict(row)
        for series_id in analysis_strategy_names:
            combined_rows_by_series.setdefault(
                series_id,
                _unavailable_factor_row(series_id, series_to_reason[series_id]),
            )
        rows = [
            combined_rows_by_series[name] for name in sorted(combined_rows_by_series)
        ]
    for row in rows:
        if (
            row["status"] == "UNAVAILABLE"
            and not row["unavailable_reasons"]
            and current_plan
        ):
            reason = next(
                (
                    disposition_reasons.get(row["series_id"], {}).get(source_key)
                    for source_key in source_keys
                    if disposition_reasons.get(row["series_id"], {}).get(source_key)
                ),
                None,
            )
            if reason:
                row["unavailable_reasons"] = [reason]
                row["selection_status"] = reason
    source_identity = [
        {
            "analytics_hash": item["analytics_hash"],
            "as_of_date": item["as_of_date"],
            "generation_id": item["generation_id"],
            "plan_spec": item.get("plan_spec"),
            "source_key": item["source_key"],
            "source_label": item["source_label"],
            "source_sha256": item["source_sha256"],
            "surface": surface,
        }
        for item in analyses
    ]
    identity_payload = {
        "lens_id": lens_id,
        "plan_bundle_id": plan_bundle_id,
        "sources": source_identity,
        "surface": surface,
    }
    composite_hash = hashlib.sha256(canonical_json_bytes(identity_payload)).hexdigest()
    available_count = sum(row["status"] == "AVAILABLE" for row in rows)
    governed_proxies = [
        {
            **proxy,
            "source_key": analysis["source_key"],
            "source_label": analysis["source_label"],
        }
        for analysis in analyses
        for proxy in _objects(
            _object(analysis.get("proxy_governance"), "analysis.proxy_governance").get(
                "proxies"
            ),
            "analysis.proxy_governance.proxies",
        )
    ]
    return {
        "analytics_hash": composite_hash,
        "analytics_schema_version": max(
            int(item["analytics_schema_version"]) for item in analyses
        ),
        "as_of_date": min(item["as_of_date"] for item in analyses),
        "as_of_policy": "MINIMUM_ACCEPTED_SOURCE_DATE",
        "available_count": available_count,
        "available_source_keys": list(source_keys),
        "default_source_key": source_keys[0],
        "diagnostic_count": sum(row["status"] == "DIAGNOSTIC_ONLY" for row in rows),
        "dynamic_available_count": sum(
            row.get("estimator") == "DYNAMIC_FLS" and row.get("status") == "AVAILABLE"
            for row in rows
        ),
        "evidence_mode": evidence_mode,
        "generation_id": composite_hash,
        "lens_id": lens_id,
        "lens_label": label,
        "method_label": analyses[0]["method_label"],
        "methodology_manifest_sha256": analyses[0]["methodology_manifest_sha256"],
        "methodology_version": analyses[0]["methodology_version"],
        "plan_bundle_id": plan_bundle_id,
        "proxy_governance": {
            "candidate_count": len(governed_proxies),
            "orthogonality_claim": "RAW_CANDIDATES_NOT_ASSUMED_ORTHOGONAL",
            "proxies": governed_proxies,
            "selection_boundary": (
                "TRAIN_ONLY_TIME_ORDERED_VALIDATION_WITH_"
                "COLLINEARITY_AND_PARSIMONY_GATES"
            ),
        },
        "return_basis": return_basis,
        "series": rows,
        "source_sha256": composite_hash,
        "sources": source_identity,
        "surface": surface,
        "unavailable_count": len(rows) - available_count,
    }


def _decomposition_lenses_for_mode(
    root: Path,
    plan_bundle: Path,
    strategy_names: Mapping[str, str],
    analysis_strategy_names: set[str],
    evidence_mode: str,
    contract_snapshot: bytes | None = None,
) -> tuple[dict[str, Any], tuple[tuple[Path, str], ...]]:
    manifest_path = plan_bundle / "plan-manifest.json"
    plan_path = (
        plan_bundle
        / "inputs"
        / "governance"
        / "current-strategy-decomposition-plan.json"
    )
    dispositions_path = plan_bundle / "candidate-dispositions.json"
    manifest, _manifest_content, manifest_sha256 = _json_snapshot(
        manifest_path, "decomposition plan manifest"
    )
    plan, _plan_content, plan_sha256 = _json_snapshot(
        plan_path, "current decomposition plan"
    )
    dispositions, _dispositions_content, dispositions_sha256 = _json_snapshot(
        dispositions_path, "candidate dispositions"
    )
    current_plan = manifest.get(
        "builder"
    ) == "portfolio-current-decomposition-plan" and (
        manifest.get("builder_version"),
        manifest.get("schema_version"),
    ) in {("0.17.0", 2), ("0.18.0", 2)}
    if current_plan:
        if manifest.get("bundle_id") != plan_bundle.name:
            raise ControlPanelValidationError(
                "current decomposition plan identity differs"
            )
    elif (
        manifest.get("schema_version") != 1
        or manifest.get("bundle_id") != plan_bundle.name
    ):
        raise ControlPanelValidationError("decomposition plan identity differs")
    expected_plan_schema = 3 if current_plan else 1
    expected_dispositions_schema = 2 if current_plan else 1
    if (
        plan.get("schema_version") != expected_plan_schema
        or dispositions.get("schema_version") != expected_dispositions_schema
    ):
        raise ControlPanelValidationError("decomposition plan schema is unsupported")
    manifest_artifacts = {
        _text(item.get("path"), "plan artifact path"): _text(
            item.get("sha256"), "plan artifact sha256"
        )
        for item in (
            _objects(manifest.get("inputs"), "plan inputs")
            + _objects(manifest.get("outputs"), "plan outputs")
        )
    }
    expected_plan_path = "inputs/governance/current-strategy-decomposition-plan.json"
    if (
        manifest_artifacts.get(expected_plan_path) != plan_sha256
        or manifest_artifacts.get("candidate-dispositions.json") != dispositions_sha256
    ):
        raise ControlPanelValidationError("decomposition plan artifact hash differs")

    planned_strategies = _objects(plan.get("strategies"), "plan strategies")
    planned_by_id = {
        _text(item.get("strategy_id"), "plan strategy_id"): item
        for item in planned_strategies
    }
    if len(planned_by_id) != len(planned_strategies):
        raise ControlPanelValidationError(
            "decomposition plan contains duplicate strategy identities"
        )
    planned_ids = set(planned_by_id)
    governed_current_ids = set(strategy_names) & CURRENT_REQUIRED_STRATEGY_IDS
    if not current_plan and governed_current_ids == CURRENT_REQUIRED_STRATEGY_IDS:
        # Schema-1 bundles predate the seven-strategy current perimeter and may
        # retain exactly the two known historical product entries. They may
        # not silently add arbitrary series or omit a current strategy.
        allowed_legacy_ids = {
            frozenset(CURRENT_REQUIRED_STRATEGY_IDS),
            frozenset(CURRENT_PRODUCT_STRATEGY_IDS),
        }
        if frozenset(planned_ids) not in allowed_legacy_ids:
            raise ControlPanelValidationError(
                "legacy decomposition plan differs from the governed strategy identities"
            )
    elif (
        planned_ids != set(strategy_names)
        and not (
            current_plan
            and CURRENT_PRODUCT_STRATEGY_IDS <= planned_ids
            and set(strategy_names) == CURRENT_REQUIRED_STRATEGY_IDS
        )
        # the plan also carries the contract's retained history-only records (#567)
        and not (
            current_plan
            and set(
                _plan_without_retained_records(
                    dict.fromkeys(planned_ids, ""), contract_snapshot
                )
            )
            == set(strategy_names)
        )
    ):
        # The accepted plan retains the two explicitly excluded historical rows;
        # the current report/panel projects the seven required strategies.
        raise ControlPanelValidationError(
            "decomposition plan differs from current strategy identities"
        )
    series_to_reason: dict[str, str] = {}
    disposition_reasons: dict[str, dict[str, str]] = {}
    for strategy_id, strategy_name in strategy_names.items():
        item = planned_by_id[strategy_id]
        if (not current_plan or strategy_id in governed_current_ids) and _text(
            item.get("series_id"), "plan series_id"
        ) != strategy_name:
            raise ControlPanelValidationError(
                f"decomposition plan series differs: {strategy_id}"
            )
        if current_plan:
            raw_cells = _object(
                item.get("source_dispositions"), "plan source_dispositions"
            )
            cells: dict[str, str] = {}
            for cell_source_key, raw_cell in raw_cells.items():
                cells[_text(cell_source_key, "plan source_key")] = _text(
                    _object(raw_cell, "plan source disposition").get("reason"),
                    "plan disposition reason",
                )
            disposition_reasons[strategy_name] = cells
            ordered_default = next(
                (
                    cells[key]
                    for key, _label in DECOMPOSITION_LENS_SOURCES.items()
                    if key in cells
                ),
                "UNAVAILABLE_ABSENT_FROM_ALL_DECOMPOSITION_LENSES",
            )
            series_to_reason[strategy_name] = ordered_default
        else:
            series_to_reason[strategy_name] = _text(
                item.get("default_disposition"), "plan default_disposition"
            )
    legacy_excluded_names = {
        _text(planned_by_id[strategy_id].get("series_id"), "plan series_id")
        for strategy_id in CURRENT_EXCLUDED_STRATEGY_IDS
        if strategy_id in planned_by_id
    }
    allowed_lens_names = set(strategy_names.values()) | legacy_excluded_names

    analyses_by_surface: dict[str, dict[str, dict[str, Any]]] = {
        "STATIC": {},
        **({"DYNAMIC_FLS": {}} if current_plan else {}),
    }
    source_bindings: list[tuple[Path, str]] = [
        (manifest_path, manifest_sha256),
        (plan_path, plan_sha256),
        (dispositions_path, dispositions_sha256),
    ]

    def add_generation_bindings(
        artifact_path: Path, identity: Mapping[str, str]
    ) -> None:
        source_bindings.extend(
            (
                (artifact_path, identity["sha256"]),
                (Path(identity["manifest_path"]), identity["manifest_sha256"]),
                (Path(identity["pointer_path"]), identity["pointer_sha256"]),
            )
        )

    surface_inputs: tuple[tuple[str, dict[str, Any], dict[str, str], Path], ...]
    for source_key, (expected_lens, source_label) in DECOMPOSITION_LENS_SOURCES.items():
        if current_plan:
            mode_root = root / evidence_mode
            if not mode_root.is_dir() or mode_root.is_symlink():
                raise ControlPanelValidationError(
                    f"current plan requires a {evidence_mode} lens layout"
                )
            static_root = mode_root / "STATIC" / source_key
            dynamic_surface_root = mode_root / "DYNAMIC_FLS" / source_key
            if not dynamic_surface_root.is_dir() or dynamic_surface_root.is_symlink():
                raise ControlPanelValidationError(
                    f"current plan requires the DYNAMIC_FLS surface: {source_key}"
                )
            decomposition, identity, path = _current_analytics(
                static_root,
                filename="decomposition.json",
                field_name=f"{source_label} STATIC decomposition",
                expected_schema_version=(1, 2, 3),
            )
            dynamic, dynamic_identity, dynamic_path = _current_analytics(
                dynamic_surface_root,
                filename="decomposition.json",
                field_name=f"{source_label} DYNAMIC_FLS decomposition",
                expected_schema_version=(1, 2, 3),
            )
            surface_inputs = (
                ("STATIC", decomposition, identity, path),
                ("DYNAMIC_FLS", dynamic, dynamic_identity, dynamic_path),
            )
        else:
            decomposition, identity, path = _current_analytics(
                root / source_key,
                filename="decomposition.json",
                field_name=f"{source_label} decomposition",
                expected_schema_version=(1, 2, 3),
            )
            surface_inputs = (("STATIC", decomposition, identity, path),)
        for surface, surface_value, surface_identity, surface_path in surface_inputs:
            add_generation_bindings(surface_path, surface_identity)
            plan_spec = (
                verify_decomposition_surface_against_plan(
                    plan_bundle,
                    source_key,
                    surface,
                    evidence_mode,
                    surface_value,
                    Path(surface_identity["manifest_path"]),
                )
                if current_plan
                else None
            )
            if plan_spec is not None:
                source_bindings.append(
                    (
                        plan_bundle / Path(plan_spec["spec_path"]),
                        _text(plan_spec["sha256"], "spec sha256"),
                    )
                )
            if surface_value.get("analysis_lens") != expected_lens:
                raise ControlPanelValidationError(
                    f"decomposition lens differs for {source_key}"
                )
            declared_mode = surface_value.get("evidence_mode", "LIVE_ONLY")
            if declared_mode != evidence_mode:
                raise ControlPanelValidationError(
                    f"decomposition evidence mode differs for {source_key}"
                )
            selected_names = {
                _text(item.get("series_id"), "lens selection series_id")
                for item in _objects(
                    surface_value.get("model_selections"), "lens model selections"
                )
            }
            if not selected_names or not selected_names <= allowed_lens_names:
                raise ControlPanelValidationError(
                    f"decomposition series boundary differs for {source_key}"
                )
            filtered_decomposition, selected_analysis_names = (
                _factor_decomposition_perimeter(surface_value, analysis_strategy_names)
            )
            analysis = _factor_analysis(
                filtered_decomposition, surface_identity, selected_analysis_names
            )
            analysis["analysis_lens"] = expected_lens
            analysis["plan_spec"] = plan_spec
            analysis["source_key"] = source_key
            analysis["source_label"] = source_label
            analysis["surface"] = surface
            analysis["series"] = [
                {
                    **row,
                    "source_key": source_key,
                    "source_label": source_label,
                    "surface": surface,
                }
                for row in analysis["series"]
            ]
            analyses_by_surface[surface][source_key] = analysis

    grouped_by_surface: dict[str, list[dict[str, Any]]] = {}
    for surface, source_analyses in analyses_by_surface.items():
        grouped_by_surface[surface] = [
            _group_decomposition_lens_sources(
                lens_id=lens_id,
                label=label,
                source_keys=source_keys,
                return_basis=return_basis,
                source_analyses=source_analyses,
                analysis_strategy_names=analysis_strategy_names,
                series_to_reason=series_to_reason,
                disposition_reasons=disposition_reasons,
                current_plan=current_plan,
                plan_bundle_id=plan_bundle.name,
                evidence_mode=evidence_mode,
                surface=surface,
            )
            for lens_id, label, source_keys, return_basis in DECOMPOSITION_LENS_GROUPS
        ]
    grouped: list[dict[str, Any]] = []
    for index, default_lens in enumerate(grouped_by_surface["STATIC"]):
        surfaces = {
            surface: rows[index] for surface, rows in grouped_by_surface.items()
        }
        grouped.append(
            {
                **default_lens,
                "available_surfaces": list(surfaces),
                "default_surface": "STATIC",
                "surfaces": surfaces,
            }
        )
    return (
        {
            "default_lens": "ECONOMIC_FACTOR",
            "lenses": grouped,
            "plan_bundle_id": plan_bundle.name,
            "plan_manifest_sha256": manifest_sha256,
        },
        tuple(source_bindings),
    )


def _decomposition_lenses(
    root: Path,
    plan_bundle: Path,
    strategy_names: Mapping[str, str],
    contract_snapshot: bytes | None = None,
    *,
    include_hybrid: bool = True,
    hybrid_plan_bundle: Path | None = None,
) -> tuple[dict[str, Any], tuple[tuple[Path, str], ...]]:
    analysis_strategy_names = _dependence_strategy_names(
        strategy_names, contract_snapshot
    )
    live, live_bindings = _decomposition_lenses_for_mode(
        root,
        plan_bundle,
        strategy_names,
        analysis_strategy_names,
        "LIVE_ONLY",
        contract_snapshot,
    )
    modes: dict[str, list[dict[str, Any]]] = {"LIVE_ONLY": live["lenses"]}
    all_bindings = list(live_bindings)
    manifest, _content, _sha256 = _json_snapshot(
        plan_bundle / "plan-manifest.json", "decomposition plan manifest"
    )
    current_plan = manifest.get("schema_version") == 2
    hybrid_root = root / "LIVE_PLUS_BACKTEST"
    if hybrid_plan_bundle is not None and (not current_plan or not include_hybrid):
        raise ControlPanelValidationError(
            "hybrid plan requires current hybrid analysis"
        )
    if hybrid_plan_bundle is not None and (
        not hybrid_root.is_dir() or hybrid_root.is_symlink()
    ):
        raise ControlPanelValidationError(
            "hybrid decomposition generation root is absent"
        )
    if include_hybrid and hybrid_root.is_dir() and not hybrid_root.is_symlink():
        if current_plan and hybrid_plan_bundle is None:
            raise ControlPanelValidationError(
                "current hybrid decomposition plan is absent"
            )
        selected_hybrid_plan = hybrid_plan_bundle if current_plan else plan_bundle
        assert selected_hybrid_plan is not None
        hybrid, hybrid_bindings = _decomposition_lenses_for_mode(
            root if current_plan else hybrid_root,
            selected_hybrid_plan,
            strategy_names,
            analysis_strategy_names,
            "LIVE_PLUS_BACKTEST",
            contract_snapshot,
        )
        if not current_plan:
            dynamic_root = hybrid_root / "dynamic-style"
            if not dynamic_root.is_dir() or dynamic_root.is_symlink():
                raise ControlPanelValidationError(
                    "hybrid dynamic-style decomposition is absent"
                )
            dynamic, dynamic_identity, dynamic_path = _current_analytics(
                dynamic_root,
                filename="decomposition.json",
                field_name="hybrid dynamic-style decomposition",
                expected_schema_version=(1, 2, 3),
            )
            if dynamic.get("evidence_mode") != "LIVE_PLUS_BACKTEST":
                raise ControlPanelValidationError(
                    "hybrid dynamic-style evidence mode differs"
                )
            filtered_dynamic, selected_dynamic_names = _factor_decomposition_perimeter(
                dynamic, analysis_strategy_names
            )
            if selected_dynamic_names != analysis_strategy_names:
                raise ControlPanelValidationError(
                    "hybrid dynamic factor series differ from the governed product perimeter"
                )
            hybrid["lenses"] = [
                *hybrid["lenses"],
                _dynamic_factor_lens(
                    filtered_dynamic,
                    dynamic_identity,
                    analysis_strategy_names,
                ),
            ]
        modes["LIVE_PLUS_BACKTEST"] = hybrid["lenses"]
        all_bindings.extend(hybrid_bindings)
        if not current_plan:
            all_bindings.extend(
                (
                    (dynamic_path, dynamic_identity["sha256"]),
                    (
                        Path(dynamic_identity["manifest_path"]),
                        dynamic_identity["manifest_sha256"],
                    ),
                    (
                        Path(dynamic_identity["pointer_path"]),
                        dynamic_identity["pointer_sha256"],
                    ),
                )
            )
    unique_bindings: dict[Path, str] = {}
    for path, sha256 in all_bindings:
        previous = unique_bindings.setdefault(path, sha256)
        if previous != sha256:
            raise ControlPanelValidationError(
                f"decomposition lens input changed during render: {path}"
            )
    return (
        {
            **live,
            "default_evidence_mode": "LIVE_ONLY",
            "evidence_modes": list(modes),
            "modes": modes,
        },
        tuple(unique_bindings.items()),
    )


def _decomposition_plan_strategy_mapping(plan_bundle: Path) -> dict[str, str]:
    """Read the already-bound plan mapping for downstream source projection."""
    manifest_path = plan_bundle / "plan-manifest.json"
    plan_path = (
        plan_bundle
        / "inputs"
        / "governance"
        / "current-strategy-decomposition-plan.json"
    )
    manifest, _manifest_bytes, _manifest_sha = _json_snapshot(
        manifest_path, "decomposition plan manifest"
    )
    plan, _plan_bytes, plan_sha = _json_snapshot(
        plan_path, "current decomposition plan"
    )
    declared = {
        _text(item.get("path"), "plan artifact path"): _text(
            item.get("sha256"), "plan artifact sha256"
        )
        for item in _objects(manifest.get("inputs"), "plan inputs")
        + _objects(manifest.get("outputs"), "plan outputs")
    }
    if (
        declared.get("inputs/governance/current-strategy-decomposition-plan.json")
        != plan_sha
    ):
        raise ControlPanelValidationError("decomposition plan artifact hash differs")
    mapping: dict[str, str] = {}
    for item in _objects(plan.get("strategies"), "plan strategies"):
        strategy_id = _text(item.get("strategy_id"), "plan strategy_id")
        if strategy_id in mapping:
            raise ControlPanelValidationError(
                "decomposition plan contains duplicate strategy identities"
            )
        mapping[strategy_id] = _text(item.get("series_id"), "plan series_id")
    return mapping


def _weekly_return_analysis(
    weekly: Mapping[str, Any],
    identity: Mapping[str, str],
    strategy_names: Mapping[str, str] | None = None,
    plan_strategy_names: Mapping[str, str] | None = None,
    *,
    include_hybrid: bool = True,
) -> dict[str, Any]:
    weekly_schema_version = weekly.get("analytics_schema_version")
    if weekly_schema_version not in {1, 2}:
        raise ControlPanelValidationError("weekly-return schema is unsupported")
    if weekly.get("default_evidence_mode") != "LIVE_ONLY":
        raise ControlPanelValidationError("weekly returns must default to LIVE_ONLY")
    modes = weekly.get("strategy_evidence_modes")
    if include_hybrid and modes != ["LIVE_ONLY", "LIVE_PLUS_BACKTEST"]:
        raise ControlPanelValidationError("weekly strategy evidence modes differ")
    if not include_hybrid and (not isinstance(modes, list) or "LIVE_ONLY" not in modes):
        raise ControlPanelValidationError("weekly LIVE_ONLY evidence mode is absent")
    week_policy = _object(weekly.get("week_policy"), "weekly week_policy")
    if (
        week_policy.get("aggregation") != "PRODUCT_OF_ONE_PLUS_DAILY_RETURN_MINUS_ONE"
        or week_policy.get("absent_value") != "UNAVAILABLE_NOT_ZERO"
    ):
        raise ControlPanelValidationError("weekly aggregation policy differs")
    current_week = _object(weekly.get("current_week"), "weekly current_week")
    if (
        weekly_schema_version == 2
        and weekly.get("status") != "UNAVAILABLE_NOT_CONFIGURED"
    ):
        try:
            as_of = date.fromisoformat(
                _text(weekly.get("as_of_date"), "weekly as_of_date")
            )
            week_start = as_of - timedelta(days=as_of.weekday())
            week_end = week_start + timedelta(days=6)
        except ValueError as error:
            raise ControlPanelValidationError(
                "weekly source cutoff is invalid"
            ) from error
        if (
            current_week.get("default_visible") is not True
            or current_week.get("week_start") != week_start.isoformat()
            or current_week.get("week_end") != week_end.isoformat()
            or current_week.get("data_through_date") != as_of.isoformat()
            or current_week.get("is_partial") is not (as_of < week_end)
            or not isinstance(weekly.get("weeks"), list)
            or week_start.isoformat() not in weekly["weeks"]
        ):
            raise ControlPanelValidationError(
                "weekly current week differs from the source cutoff"
            )
    weeks_value = weekly.get("weeks")
    if not isinstance(weeks_value, list) or not weeks_value:
        raise ControlPanelValidationError("weekly week boundary is empty")
    weeks = [_text(item, "weekly week") for item in weeks_value]
    if weeks != sorted(set(weeks)):
        raise ControlPanelValidationError("weekly week identities differ")
    rows = _objects(weekly.get("series"), "weekly series")
    series_ids = [_text(row.get("series_id"), "weekly series_id") for row in rows]
    if len(series_ids) != len(set(series_ids)):
        raise ControlPanelValidationError("weekly series identity is duplicated")
    if strategy_names is not None:
        current_ids = set(strategy_names)
        if current_ids != CURRENT_REQUIRED_STRATEGY_IDS:
            raise ControlPanelValidationError(
                "weekly strategy identity authority differs"
            )
        authoritative_names = dict(strategy_names)
        if plan_strategy_names is not None:
            plan_ids = set(plan_strategy_names)
            if plan_ids not in (
                set(CURRENT_REQUIRED_STRATEGY_IDS),
                set(CURRENT_PRODUCT_STRATEGY_IDS),
            ):
                raise ControlPanelValidationError(
                    "weekly strategy identity authority differs"
                )
            plan_names = list(plan_strategy_names.values())
            if len(plan_names) != len(set(plan_names)):
                raise ControlPanelValidationError(
                    "weekly strategy display identity is duplicated"
                )
            for strategy_id in CURRENT_REQUIRED_STRATEGY_IDS:
                if strategy_names[strategy_id] != plan_strategy_names[strategy_id]:
                    raise ControlPanelValidationError(
                        "weekly strategy mapping differs from plan"
                    )
            authoritative_names = dict(plan_strategy_names)
        raw_strategy_rows = [
            row for row in rows if row.get("primary_group") == "STRATEGY"
        ]
        raw_names = [
            _text(row.get("display_name"), "weekly strategy name")
            for row in raw_strategy_rows
        ]
        if len(raw_names) != len(set(raw_names)):
            raise ControlPanelValidationError(
                "weekly strategy display identity is duplicated"
            )
        raw_name_set = set(raw_names)
        expected_names = set(authoritative_names.values())
        current_names = set(strategy_names.values())
        if raw_name_set not in (current_names, expected_names):
            raise ControlPanelValidationError(
                "weekly strategy display identity differs"
            )
        raw_name_to_id = {
            name: strategy_id for strategy_id, name in authoritative_names.items()
        }
        for row in raw_strategy_rows:
            name = _text(row.get("display_name"), "weekly strategy name")
            if name not in raw_name_to_id:
                raise ControlPanelValidationError(
                    "weekly strategy display identity is unknown"
                )
            source_id = _text(row.get("series_id"), "weekly strategy_id")
            if source_id not in {name, f"strategy:{name}"}:
                raise ControlPanelValidationError(
                    "weekly strategy source identity differs"
                )
        rows = [
            row
            for row in rows
            if row.get("primary_group") != "STRATEGY"
            or raw_name_to_id[_text(row.get("display_name"), "weekly strategy name")]
            in CURRENT_REQUIRED_STRATEGY_IDS
        ]
        weekly = {**weekly, "series": rows}
    strategy_count = sum(row.get("primary_group") == "STRATEGY" for row in rows)
    benchmark_count = sum(
        "CORE_BENCHMARK" in row.get("display_groups", []) for row in rows
    )
    required_groups = {
        "STRATEGY",
        "ECONOMIC_FACTOR_US",
        "ECONOMIC_FACTOR_EUROPE",
        "STYLE_US_EQUITY",
        "STYLE_MULTI_ASSET",
        "SECTOR",
        "INDUSTRY",
        "CORE_BENCHMARK",
    }
    if strategy_count != 7:
        raise ControlPanelValidationError("weekly return strategy count must be 7")
    if benchmark_count != CORE_BENCHMARK_COUNT:
        raise ControlPanelValidationError(
            "weekly strategy or benchmark boundary differs"
        )
    if set(weekly.get("display_groups", [])) != required_groups:
        raise ControlPanelValidationError("weekly display group boundary differs")
    meta_fields = weekly.get("observation_meta_fields")
    if meta_fields != [
        "daily_observation_count",
        "actual_observed_start",
        "actual_observed_end",
        "coverage_status",
        "boundary_reasons",
    ]:
        raise ControlPanelValidationError("weekly observation metadata fields differ")
    for row in rows:
        if row.get("primary_group") == "STRATEGY":
            values_by_mode = _object(row.get("values_by_mode"), "weekly values by mode")
            meta_by_mode = _object(
                row.get("observation_meta_by_mode"), "weekly metadata by mode"
            )
            required_modes = (
                set(STRATEGY_EVIDENCE_MODES) if include_hybrid else {"LIVE_ONLY"}
            )
            if include_hybrid and (
                set(values_by_mode) != required_modes
                or set(meta_by_mode) != required_modes
            ):
                raise ControlPanelValidationError("weekly strategy modes differ")
            if not include_hybrid and (
                "LIVE_ONLY" not in values_by_mode or "LIVE_ONLY" not in meta_by_mode
            ):
                raise ControlPanelValidationError(
                    "weekly LIVE_ONLY strategy mode differs"
                )
            for mode in STRATEGY_EVIDENCE_MODES if include_hybrid else ("LIVE_ONLY",):
                mode_values = _object(values_by_mode.get(mode), f"weekly values {mode}")
                mode_meta = _object(meta_by_mode.get(mode), f"weekly metadata {mode}")
                if set(mode_values) != set(mode_meta):
                    raise ControlPanelValidationError(
                        "weekly strategy value and metadata weeks differ"
                    )
        elif set(row.get("values", {})) != set(row.get("observation_meta", {})):
            raise ControlPanelValidationError(
                "weekly market value and metadata weeks differ"
            )
    accepted_weekly = dict(weekly)
    if not include_hybrid:
        accepted_rows: list[dict[str, Any]] = []
        for raw in rows:
            row = copy.deepcopy(raw)
            if row.get("primary_group") == "STRATEGY":
                for field in (
                    "calendar_basis_by_mode",
                    "mode_coverage",
                    "observation_meta_by_mode",
                    "values_by_mode",
                ):
                    value = row.get(field)
                    if isinstance(value, dict) and "LIVE_ONLY" in value:
                        row[field] = {"LIVE_ONLY": value["LIVE_ONLY"]}
            accepted_rows.append(row)
        accepted_weekly["series"] = accepted_rows
        accepted_weekly["strategy_evidence_modes"] = ["LIVE_ONLY"]
    return {
        **accepted_weekly,
        "generation_id": identity["generation_id"],
        "source_sha256": identity["sha256"],
    }


def _validate_hybrid_decomposition_lenses(
    hybrid_lenses: list[dict[str, Any]], lens_ids: set[str]
) -> None:
    """Accept current grouped surfaces or the existing separate legacy lens."""
    grouped_dynamic = all(
        isinstance(lens.get("surfaces"), Mapping)
        and set(lens["surfaces"]) == {"STATIC", "DYNAMIC_FLS"}
        and all(
            isinstance(lens["surfaces"][surface], Mapping)
            and lens["surfaces"][surface].get("surface") == surface
            and bool(lens["surfaces"][surface].get("series"))
            for surface in ("STATIC", "DYNAMIC_FLS")
        )
        for lens in hybrid_lenses
    )
    expected = lens_ids if grouped_dynamic else lens_ids | {"DYNAMIC_STYLE"}
    if {
        _text(row.get("lens_id"), "hybrid decomposition lens_id")
        for row in hybrid_lenses
    } != expected:
        raise ControlPanelValidationError("hybrid decomposition mode lens set differs")


def _hybrid_mode_gate(strategies: list[dict[str, Any]]) -> dict[str, Any]:
    blocked = []
    eligible = []
    for row in strategies:
        history_status = row.get("history_compatibility_status")
        if history_status is not None:
            # The gate reports the report's own version registry status; it never
            # relabels accepted history as PROVEN_COMPATIBLE. History accepted
            # beside an unbound registry stays blocked and shows that status.
            registry_status = str(
                row.get("version_compatibility_status") or "UNRESOLVED"
            )
            accepted = (
                history_status == "ACCEPTED_FOR_ECONOMIC_INFERENCE"
                and row.get("live_plus_backtest_status") == "ELIGIBLE_ACCEPTED_HISTORY"
                and registry_status == "PROVEN_COMPATIBLE"
            )
            compatibility = (
                registry_status
                if history_status == "ACCEPTED_FOR_ECONOMIC_INFERENCE"
                else str(history_status)
            )
            reason = str(
                row.get("live_plus_backtest_reason")
                or row.get("history_compatibility_reason")
                or "No accepted strategy history is recorded."
            )
            if (
                history_status == "ACCEPTED_FOR_ECONOMIC_INFERENCE"
                and registry_status != "PROVEN_COMPATIBLE"
            ):
                reason = (
                    "Accepted history is not bound to a compatible version "
                    f"registry row ({registry_status}): "
                    f"{row.get('version_compatibility_reason') or 'no reason recorded'}"
                )
            version_ref = row.get("history_version_ref")
        else:
            compatibility = str(row.get("version_compatibility_status") or "UNRESOLVED")
            report_status = str(
                row.get("live_plus_backtest_status")
                or "BLOCKED_VERSION_NOT_PROVEN_COMPATIBLE"
            )
            accepted = (
                compatibility == "PROVEN_COMPATIBLE"
                and report_status == "ELIGIBLE_PROVEN_COMPATIBLE"
            )
            reason = str(
                row.get("live_plus_backtest_reason")
                or row.get("version_compatibility_reason")
                or "No effective-dated compatibility evidence is recorded."
            )
            version_ref = (
                row.get("backtest_version_ref")
                if "backtest_version_ref" in row
                else row.get("version_ref")
            )
        if not accepted:
            blocked.append(
                {
                    "compatibility_status": compatibility,
                    "reason": reason,
                    "strategy_id": _text(
                        row.get("strategy_id"), "strategy.strategy_id"
                    ),
                    "strategy_name": _text(
                        row.get("strategy_name"), "strategy.strategy_name"
                    ),
                }
            )
        else:
            eligible.append(
                {
                    "compatibility_status": compatibility,
                    "reason": reason,
                    "strategy_id": _text(
                        row.get("strategy_id"), "strategy.strategy_id"
                    ),
                    "strategy_name": _text(
                        row.get("strategy_name"), "strategy.strategy_name"
                    ),
                    "version_ref": _text(version_ref, "strategy history version_ref"),
                }
            )
    if blocked:
        if eligible:
            return {
                "blocked_strategies": blocked,
                "eligible_strategies": eligible,
                "reason": (
                    f"LIVE_PLUS_BACKTEST is enabled independently for {len(eligible)} "
                    f"strategy history path(s); {len(blocked)} strategy history "
                    "path(s) remain unavailable and live-only. Implementation "
                    "reconciliation is evaluated separately."
                ),
                "status": "ELIGIBLE_PARTIAL_VERSION_COMPATIBILITY",
            }
        return {
            "blocked_strategies": blocked,
            "eligible_strategies": [],
            "reason": (
                f"LIVE_PLUS_BACKTEST is unavailable because {len(blocked)} current "
                "strategy history path(s) are not accepted for economic inference."
            ),
            "status": "BLOCKED_VERSION_NOT_PROVEN_COMPATIBLE",
        }
    return {
        "blocked_strategies": [],
        "eligible_strategies": eligible,
        "reason": (
            "Every current strategy has an accepted, version-bound history path for "
            "economic inference; implementation reconciliation remains separate."
        ),
        "status": "ELIGIBLE_PROVEN_COMPATIBLE",
    }


def _report_hybrid_mode_gate(
    report: Mapping[str, Any], contract_snapshot: bytes | None
) -> tuple[dict[str, Any], bool]:
    """Derive the global analytics gate before any hybrid source is opened."""

    controls = _financial_mvp_product_controls(contract_snapshot)
    current = _current_product_rows(
        _objects(report.get("strategy_summary"), "strategy_summary"), controls
    )
    current_names = {
        _text(row.get("strategy_id"), "strategy.strategy_id"): _text(
            row.get("strategy_name"), "strategy.strategy_name"
        )
        for row in current
    }
    dependence_names = _dependence_strategy_names(current_names, contract_snapshot)
    gate = _hybrid_mode_gate(
        [
            row
            for row in current
            if _text(row.get("strategy_name"), "strategy.strategy_name")
            in dependence_names
        ]
    )
    # Partial coverage opens the hybrid sources; the per-strategy readers below
    # keep a blocked strategy live-only inside the mode. See the note beside
    # the builder's gate for why requiring full coverage here stripped the mode
    # from the whole release.
    globally_eligible = gate["status"] in {
        "ELIGIBLE_PROVEN_COMPATIBLE",
        "ELIGIBLE_PARTIAL_VERSION_COMPATIBILITY",
    }
    return gate, globally_eligible


def _govern_weekly_hybrid_modes(
    weekly: Mapping[str, Any], gate: Mapping[str, Any]
) -> dict[str, Any]:
    source_modes = weekly.get("strategy_evidence_modes", [])
    hybrid_configured = (
        weekly.get("generation_id") is not None
        and isinstance(source_modes, list)
        and "LIVE_PLUS_BACKTEST" in source_modes
    )
    # Partial coverage is expressed per strategy below: an eligible strategy
    # keeps its hybrid coverage and must match the registry version, and a
    # blocked one keeps its live history with no hybrid value, calendar,
    # metadata or coverage claim. Requiring a globally clean gate here skipped
    # that machinery entirely and stripped the mode from every strategy.
    hybrid_eligible = bool(gate.get("eligible_strategies")) and gate["status"] in {
        "ELIGIBLE_PROVEN_COMPATIBLE",
        "ELIGIBLE_PARTIAL_VERSION_COMPATIBILITY",
    }
    accepted = {
        **weekly,
        "hybrid_mode_reason": (
            gate["reason"]
            if not hybrid_eligible or hybrid_configured
            else "No accepted weekly-return generation with a hybrid mode is configured."
        ),
        "hybrid_mode_status": (
            "UNAVAILABLE_NOT_CONFIGURED"
            if weekly.get("generation_id") is None
            else gate["status"]
        ),
    }
    if hybrid_eligible and hybrid_configured:
        eligible_versions = {
            _text(item.get("strategy_name"), "eligible strategy_name"): _text(
                item.get("version_ref"), "eligible version_ref"
            )
            for item in _objects(gate.get("eligible_strategies"), "eligible strategies")
        }
        accepted_rows: list[dict[str, Any]] = []
        for raw in _objects(weekly.get("series"), "weekly series"):
            row = dict(raw)
            if row.get("primary_group") == "STRATEGY":
                strategy_name = _text(
                    row.get("display_name", row.get("series_id")),
                    "weekly strategy name",
                )
                expected_version = eligible_versions.get(strategy_name)
                if expected_version is not None:
                    mode_coverage = _object(
                        row.get("mode_coverage"), "weekly mode coverage"
                    )
                    hybrid_coverage = _object(
                        mode_coverage.get("LIVE_PLUS_BACKTEST"),
                        "weekly hybrid mode coverage",
                    )
                    if hybrid_coverage.get("strategy_version") != expected_version:
                        raise ControlPanelValidationError(
                            "eligible weekly hybrid version differs from registry: "
                            f"{strategy_name}"
                        )
                else:
                    # Retain the strategy and its live history, but do not expose a
                    # hybrid value, calendar, metadata, or coverage claim.
                    for field in (
                        "calendar_basis_by_mode",
                        "mode_coverage",
                        "observation_meta_by_mode",
                        "values_by_mode",
                    ):
                        value = row.get(field)
                        if isinstance(value, dict):
                            row[field] = {"LIVE_ONLY": value.get("LIVE_ONLY", {})}
            accepted_rows.append(row)
        accepted["series"] = accepted_rows
        return accepted
    accepted["strategy_evidence_modes"] = ["LIVE_ONLY"]
    live_only_rows: list[dict[str, Any]] = []
    for raw in _objects(weekly.get("series"), "weekly series"):
        row = dict(raw)
        if row.get("primary_group") == "STRATEGY":
            for field in (
                "calendar_basis_by_mode",
                "mode_coverage",
                "observation_meta_by_mode",
                "values_by_mode",
            ):
                value = row.get(field)
                if isinstance(value, dict):
                    row[field] = (
                        {"LIVE_ONLY": value["LIVE_ONLY"]}
                        if "LIVE_ONLY" in value
                        else {}
                    )
        live_only_rows.append(row)
    accepted["series"] = live_only_rows
    return accepted


def _replication_analysis(
    replication: Mapping[str, Any],
    identity: Mapping[str, str],
    strategy_names: set[str],
    decomposition_identity: Mapping[str, str],
) -> dict[str, Any]:
    if replication.get("replication_schema_version") != 1:
        raise ControlPanelValidationError("replication schema is unsupported")
    if (
        replication.get("method_label")
        != "MPI_INFORMED_NON_EQUIVALENT_RESEARCH_REPLICATION"
    ):
        raise ControlPanelValidationError("replication method label differs")
    authority = _object(replication.get("authority_boundary"), "replication authority")
    if authority != {
        "allocation_authority": "NONE",
        "execution_authority": "NONE",
        "hwm_authority": "NONE",
        "readiness_gate_effect": "NONE",
        "use": "RESEARCH_DIAGNOSTIC_ONLY",
    }:
        raise ControlPanelValidationError("replication authority boundary differs")
    sources = _object(replication.get("sources"), "replication sources")
    if sources.get("decomposition_generation_id") != decomposition_identity.get(
        "generation_id"
    ) or sources.get("decomposition_sha256") != decomposition_identity.get("sha256"):
        raise ControlPanelValidationError(
            "replication source differs from accepted dynamic decomposition"
        )
    rows = _objects(replication.get("replication_results"), "replication results")
    by_series = {
        _text(row.get("series_id"), "replication series_id"): row for row in rows
    }
    if set(by_series) != strategy_names or len(by_series) != len(rows):
        raise ControlPanelValidationError("replication strategy boundary differs")
    available = 0
    diagnostic = 0
    unavailable = 0
    for series_id, row in by_series.items():
        status = _text(row.get("availability_status"), "replication status")
        if status == "UNAVAILABLE_NO_SELECTED_DYNAMIC_MODEL":
            blockers = row.get("blocking_reasons")
            if (
                row.get("model_id") is not None
                or not isinstance(blockers, list)
                or not blockers
                or not all(isinstance(item, str) and item.strip() for item in blockers)
            ):
                raise ControlPanelValidationError(
                    f"unavailable replication is not dispositioned: {series_id}"
                )
            unavailable += 1
            continue
        if status not in {
            "AVAILABLE_RESEARCH_REPLICATION",
            "DIAGNOSTIC_ONLY_SOURCE_MODEL",
        }:
            raise ControlPanelValidationError(
                f"replication status is unsupported: {series_id}"
            )
        metrics = _object(row.get("metrics"), f"{series_id} replication metrics")
        observation_count = metrics.get("observation_count")
        if (
            isinstance(observation_count, bool)
            or not isinstance(observation_count, int)
            or observation_count <= 0
        ):
            raise ControlPanelValidationError(
                f"replication observation boundary is empty: {series_id}"
            )
        parameter_selection = _object(
            row.get("parameter_selection"), f"{series_id} parameter selection"
        )
        if (
            parameter_selection.get("holdout_recalibration") != "NONE"
            or parameter_selection.get("nested_evaluation")
            != "TRAIN_SELECT_THEN_CAUSAL_HOLDOUT_REPLAY"
        ):
            raise ControlPanelValidationError(
                f"replication is not forward-only: {series_id}"
            )
        tracker_rows = _objects(
            row.get("tracker_rows"), f"{series_id} replication tracker"
        )
        if len(tracker_rows) != observation_count or any(
            item.get("information_set")
            != "MODEL_STATE_ESTIMATED_THROUGH_PRIOR_OBSERVATION"
            for item in tracker_rows
        ):
            raise ControlPanelValidationError(
                f"replication tracker is not causal or complete: {series_id}"
            )
        if status == "AVAILABLE_RESEARCH_REPLICATION":
            available += 1
        else:
            diagnostic += 1
    return {
        "analytics_hash": _text(
            replication.get("analytics_hash"), "replication analytics_hash"
        ),
        "as_of_date": _text(replication.get("as_of_date"), "replication as_of_date"),
        "authority_boundary": authority,
        "available_count": available,
        "clean_room_boundary": _text(
            replication.get("clean_room_boundary"), "replication clean_room_boundary"
        ),
        "diagnostic_count": diagnostic,
        "generation_id": identity["generation_id"],
        "method_label": replication["method_label"],
        "series": [by_series[name] for name in sorted(by_series)],
        "source_sha256": identity["sha256"],
        "unavailable_count": unavailable,
    }


def _project_current_product_matrix(
    raw: object,
    *,
    indices: Sequence[int],
    source_size: int,
    field_name: str,
) -> dict[str, Any]:
    matrix = copy.deepcopy(_object(raw, field_name))
    for values_field in ("input_values", "values"):
        if values_field not in matrix:
            continue
        values = matrix[values_field]
        if (
            not isinstance(values, list)
            or len(values) != source_size
            or any(
                not isinstance(row, list) or len(row) != source_size for row in values
            )
        ):
            raise ControlPanelValidationError(
                f"{field_name}.{values_field} dimensions differ"
            )
        matrix[values_field] = [
            [values[row][column] for column in indices] for row in indices
        ]
    # A principal submatrix is not the matrix on which these whole-source
    # eigenvalue/PSD diagnostics were calculated.  Silence is more truthful
    # than relabelling those diagnostics as seven-series evidence.
    matrix.pop("projection", None)
    return matrix


def _project_current_product_pairs(
    raw: object,
    *,
    retained_names: set[str],
    source_names: Sequence[str],
    field_name: str,
) -> list[dict[str, Any]]:
    items = copy.deepcopy(_objects(raw, field_name))
    expected_pairs = {
        frozenset((left, right))
        for index, left in enumerate(source_names)
        for right in source_names[index + 1 :]
    }
    observed_pairs: set[frozenset[str]] = set()
    for item in items:
        pair = frozenset(
            (
                _text(item.get("left"), f"{field_name}.left"),
                _text(item.get("right"), f"{field_name}.right"),
            )
        )
        if len(pair) != 2 or pair in observed_pairs:
            raise ControlPanelValidationError(
                f"{field_name} source pair coverage is invalid"
            )
        observed_pairs.add(pair)
    if observed_pairs != expected_pairs:
        raise ControlPanelValidationError(f"{field_name} source pair coverage differs")
    return [
        item
        for item in items
        if item.get("left") in retained_names and item.get("right") in retained_names
    ]


def _project_current_product_components(
    raw: object,
    *,
    retained_names: set[str],
    source_names: Sequence[str],
    field_name: str,
) -> list[dict[str, Any]]:
    components = copy.deepcopy(_objects(raw, field_name))
    component_ids = [
        _text(component.get("series_id"), f"{field_name}.series_id")
        for component in components
    ]
    if len(component_ids) != len(set(component_ids)) or set(component_ids) != set(
        source_names
    ):
        raise ControlPanelValidationError(
            f"{field_name} source component identities differ"
        )
    by_series_id = {component["series_id"]: component for component in components}
    return [
        by_series_id[series_id]
        for series_id in source_names
        if series_id in retained_names
    ]


def _project_current_product_dependence(
    dependence_payload: Mapping[str, Any],
    strategy_names_by_id: Mapping[str, str],
) -> dict[str, Any]:
    dependence = copy.deepcopy(dict(dependence_payload))
    mapping_ids = set(strategy_names_by_id)
    if not set(CURRENT_REQUIRED_STRATEGY_IDS) <= mapping_ids or not mapping_ids <= set(
        CURRENT_PRODUCT_STRATEGY_IDS
    ):
        raise ControlPanelValidationError(
            "current-product correlation strategy mapping identity differs"
        )
    governed_names = {
        strategy_id: _text(
            strategy_names_by_id[strategy_id],
            f"current-product correlation strategy name {strategy_id}",
        )
        for strategy_id in mapping_ids
    }
    if len(set(governed_names.values())) != len(governed_names):
        raise ControlPanelValidationError(
            "current-product correlation strategy mapping is ambiguous"
        )
    name_to_id = {name: strategy_id for strategy_id, name in governed_names.items()}
    raw_series = dependence.get("series_ids")
    if not isinstance(raw_series, list):
        raise ControlPanelValidationError(
            "current-product correlation series identity must be an array"
        )
    source_names = [
        _text(item, "current-product correlation series identity")
        for item in raw_series
    ]
    if len(source_names) != len(set(source_names)):
        raise ControlPanelValidationError(
            "current-product correlation series identity contains a duplicate"
        )
    try:
        source_ids = [name_to_id[name] for name in source_names]
    except KeyError as exc:
        raise ControlPanelValidationError(
            "current-product correlation series identity contains an unknown extra"
        ) from exc
    source_id_set = set(source_ids)
    native = len(source_ids) == len(
        CURRENT_REQUIRED_STRATEGY_IDS
    ) and source_id_set == set(CURRENT_REQUIRED_STRATEGY_IDS)
    retained_source = len(source_ids) == len(
        CURRENT_PRODUCT_STRATEGY_IDS
    ) and source_id_set == set(CURRENT_PRODUCT_STRATEGY_IDS)
    if not native and not retained_source:
        raise ControlPanelValidationError(
            "current-product correlation series identity is not exact seven or exact nine"
        )
    if native:
        return dependence

    indices = [
        index
        for index, strategy_id in enumerate(source_ids)
        if strategy_id in CURRENT_REQUIRED_STRATEGY_IDS
    ]
    retained_names = [source_names[index] for index in indices]
    retained_name_set = set(retained_names)
    source_size = len(source_names)
    dependence["series_ids"] = retained_names

    for group_name in ("matrices", "horizon_matrices", "drawdown_matrices"):
        group = _object(dependence.get(group_name), f"dependence.{group_name}")
        dependence[group_name] = {
            name: _project_current_product_matrix(
                value,
                indices=indices,
                source_size=source_size,
                field_name=f"dependence.{group_name}.{name}",
            )
            for name, value in group.items()
        }

    projected_pairs: dict[str, list[dict[str, Any]]] = {}
    for field_name in ("observed_cells", "blend_cells", "implementation_cells"):
        projected_pairs[field_name] = _project_current_product_pairs(
            dependence.get(field_name),
            retained_names=retained_name_set,
            source_names=source_names,
            field_name=f"dependence.{field_name}",
        )
        dependence[field_name] = projected_pairs[field_name]

    correlation_one = copy.deepcopy(
        _object(dependence.get("correlation_one"), "dependence.correlation_one")
    )
    correlation_components = _project_current_product_components(
        correlation_one.get("components"),
        retained_names=retained_name_set,
        source_names=source_names,
        field_name="dependence.correlation_one.components",
    )
    correlation_loss = sum(
        Decimal(
            str(
                _finite_float(
                    component.get("loss_contribution"),
                    "correlation-one loss contribution",
                )
            )
        )
        for component in correlation_components
    )
    correlation_one["components"] = correlation_components
    correlation_one["loss"] = _number_text(float(correlation_loss))
    if "declared_loss" in correlation_one:
        correlation_one["declared_loss"] = correlation_one["loss"]
    if "reconciliation_difference" in correlation_one:
        correlation_one["reconciliation_difference"] = "0"
    dependence["correlation_one"] = correlation_one

    central_case = copy.deepcopy(
        _object(dependence.get("central_case"), "dependence.central_case")
    )
    central_components = _project_current_product_components(
        central_case.get("components"),
        retained_names=retained_name_set,
        source_names=source_names,
        field_name="dependence.central_case.components",
    )
    central_case["components"] = central_components
    central_matrix = _project_current_product_matrix(
        central_case.get("dependence_matrix"),
        indices=indices,
        source_size=source_size,
        field_name="dependence.central_case.dependence_matrix",
    )
    central_case["dependence_matrix"] = central_matrix
    if central_case.get("method") != "SQRT_V_TRANSPOSE_CORRELATION_V":
        raise ControlPanelValidationError(
            "projected central-case loss method is unsupported"
        )
    central_values = central_matrix.get("values")
    if not isinstance(central_values, list):
        raise ControlPanelValidationError(
            "projected central-case dependence matrix is unavailable"
        )
    loss_vector = [
        Decimal(str(component.get("allocation_weight")))
        * Decimal(str(component.get("adverse_loss_fraction")))
        for component in central_components
    ]
    quadratic_loss = sum(
        (
            loss_vector[row]
            * Decimal(str(central_values[row][column]))
            * loss_vector[column]
            for row in range(len(loss_vector))
            for column in range(len(loss_vector))
        ),
        Decimal("0"),
    )
    if quadratic_loss < 0:
        raise ControlPanelValidationError(
            "projected central-case quadratic loss is negative"
        )
    central_case["loss"] = _number_text(float(quadratic_loss.sqrt()))
    central_case["measured_component_count"] = sum(
        component.get("status") == "MEASURED" for component in central_components
    )
    central_case["prior_component_count"] = (
        len(central_components) - central_case["measured_component_count"]
    )
    central_case["measured_pair_count"] = sum(
        item.get("status") == "AVAILABLE" for item in projected_pairs["observed_cells"]
    )
    central_case["prior_pair_count"] = (
        len(projected_pairs["observed_cells"]) - central_case["measured_pair_count"]
    )
    dependence["central_case"] = central_case

    performance = dependence.get("performance")
    if performance is not None:
        performance_rows = copy.deepcopy(_object(performance, "dependence.performance"))
        if set(performance_rows) != set(source_names):
            raise ControlPanelValidationError(
                "dependence performance source identities differ"
            )
        dependence["performance"] = {
            name: performance_rows[name] for name in retained_names
        }
    composition = dependence.get("series_composition")
    if composition is not None:
        composition_rows = copy.deepcopy(
            _object(composition, "dependence.series_composition")
        )
        if set(composition_rows) != set(source_names):
            raise ControlPanelValidationError(
                "dependence series composition source identities differ"
            )
        dependence["series_composition"] = {
            name: composition_rows[name] for name in retained_names
        }
    return dependence


def _current_product_correlation_projection(
    correlation: Mapping[str, Any],
    strategy_names_by_id: Mapping[str, str],
    *,
    include_hybrid: bool,
) -> dict[str, Any]:
    """Purely project an accepted retained correlation onto current products."""

    projected = copy.deepcopy(dict(correlation))
    raw_dependence = _object(correlation.get("dependence"), "dependence")
    source_series_ids = raw_dependence.get("series_ids")
    projected["_current_product_projection_applied"] = isinstance(
        source_series_ids, list
    ) and len(source_series_ids) == len(CURRENT_PRODUCT_STRATEGY_IDS)
    live = _project_current_product_dependence(raw_dependence, strategy_names_by_id)
    projected["dependence"] = live
    raw_modes = correlation.get("dependence_modes")
    if raw_modes is not None:
        modes = _object(raw_modes, "dependence_modes")
        if "LIVE_ONLY" not in modes or modes["LIVE_ONLY"] != raw_dependence:
            raise ControlPanelValidationError(
                "default dependence payload differs from LIVE_ONLY"
            )
        projected_modes = {"LIVE_ONLY": live}
        if include_hybrid:
            if set(modes) != {"LIVE_ONLY", "LIVE_PLUS_BACKTEST"}:
                raise ControlPanelValidationError("dependence mode set differs")
            projected_modes["LIVE_PLUS_BACKTEST"] = _project_current_product_dependence(
                _object(
                    modes["LIVE_PLUS_BACKTEST"],
                    "dependence_modes.LIVE_PLUS_BACKTEST",
                ),
                strategy_names_by_id,
            )
        projected["dependence_modes"] = projected_modes
    semantic_payload = {"dependence": live}
    if raw_modes is not None:
        semantic_payload["dependence_modes"] = projected["dependence_modes"]
    projected["analytics_hash"] = hashlib.sha256(
        canonical_json_bytes(semantic_payload)
    ).hexdigest()
    return projected


def _dependence_analysis(
    correlation: Mapping[str, Any],
    identity: Mapping[str, str],
    strategy_names: set[str],
    *,
    dependence_payload: Mapping[str, Any] | None = None,
    evidence_mode: str = "LIVE_ONLY",
    economic_contract_snapshot: bytes | None = None,
) -> dict[str, Any]:
    dependence = (
        _object(correlation.get("dependence"), "dependence")
        if dependence_payload is None
        else dict(dependence_payload)
    )
    declared_mode = dependence.get("evidence_mode")
    if declared_mode is not None and declared_mode != evidence_mode:
        raise ControlPanelValidationError(
            f"dependence mode {declared_mode!r} differs from {evidence_mode!r}"
        )
    raw_series = dependence.get("series_ids")
    if not isinstance(raw_series, list):
        raise ControlPanelValidationError("dependence.series_ids must be an array")
    series_ids = [_text(item, "dependence.series_ids[]") for item in raw_series]
    if len(series_ids) != len(strategy_names) or set(series_ids) != strategy_names:
        raise ControlPanelValidationError(
            "correlation series differ from the current strategy boundary"
        )
    matrices = _object(dependence.get("matrices"), "dependence.matrices")
    required_matrices = {
        "conceptual_prior",
        "decision_no_missing_diversification_credit",
        "observed_pearson",
        "observed_spearman",
        "posterior",
        "stress",
    }
    if set(matrices) != required_matrices:
        raise ControlPanelValidationError("dependence matrix set differs")
    for name, matrix_value in matrices.items():
        matrix = _object(matrix_value, f"matrices.{name}")
        values = matrix.get("values")
        if not isinstance(values, list) or len(values) != len(series_ids):
            raise ControlPanelValidationError(f"matrix {name} row count differs")
        if any(
            not isinstance(row, list) or len(row) != len(series_ids) for row in values
        ):
            raise ControlPanelValidationError(f"matrix {name} column count differs")

    horizon_matrices = _object(
        dependence.get("horizon_matrices"), "dependence.horizon_matrices"
    )
    if set(horizon_matrices) != set(DEPENDENCE_HORIZONS):
        raise ControlPanelValidationError("dependence horizon matrix set differs")
    drawdown_matrices = _object(
        dependence.get("drawdown_matrices"), "dependence.drawdown_matrices"
    )
    if set(drawdown_matrices) != set(DEPENDENCE_DRAWDOWN_REGIMES):
        raise ControlPanelValidationError("dependence drawdown matrix set differs")
    for group_name, group in (
        ("horizon", horizon_matrices),
        ("drawdown", drawdown_matrices),
    ):
        for name, matrix_value in group.items():
            matrix = _object(matrix_value, f"{group_name}_matrices.{name}")
            values = matrix.get("values")
            if not isinstance(values, list) or len(values) != len(series_ids):
                raise ControlPanelValidationError(
                    f"{group_name} matrix {name} row count differs"
                )
            if any(
                not isinstance(row, list) or len(row) != len(series_ids)
                for row in values
            ):
                raise ControlPanelValidationError(
                    f"{group_name} matrix {name} column count differs"
                )

    observed = _objects(dependence.get("observed_cells"), "observed_cells")
    blends = _objects(dependence.get("blend_cells"), "blend_cells")
    implementation = _objects(
        dependence.get("implementation_cells"), "implementation_cells"
    )
    expected_cells = len(series_ids) * (len(series_ids) - 1) // 2
    if not all(
        len(items) == expected_cells for items in (observed, blends, implementation)
    ):
        raise ControlPanelValidationError("dependence cell coverage differs")

    def keyed(
        items: list[dict[str, Any]], name: str
    ) -> dict[frozenset[str], dict[str, Any]]:
        result: dict[frozenset[str], dict[str, Any]] = {}
        for item in items:
            key = frozenset(
                (
                    _text(item.get("left"), f"{name}.left"),
                    _text(item.get("right"), f"{name}.right"),
                )
            )
            if len(key) != 2 or key in result:
                raise ControlPanelValidationError(f"{name} pair coverage is invalid")
            result[key] = item
        return result

    observed_index = keyed(observed, "observed")
    blend_index = keyed(blends, "blend")
    implementation_index = keyed(implementation, "implementation")
    if not (set(observed_index) == set(blend_index) == set(implementation_index)):
        raise ControlPanelValidationError("dependence pair sets differ")
    cells = [
        {
            "blend": blend_index[key],
            "implementation": implementation_index[key],
            "left": item["left"],
            "observed": item,
            "right": item["right"],
        }
        for key, item in observed_index.items()
    ]
    cells.sort(
        key=lambda item: (
            series_ids.index(item["left"]),
            series_ids.index(item["right"]),
        )
    )
    correlation_one = _object(dependence.get("correlation_one"), "correlation_one")
    raw_central_case = dependence.get("central_case")
    if raw_central_case is None:
        central_case: dict[str, Any] = {
            "classification": "UNAVAILABLE_LEGACY_ACCEPTED_GENERATION",
            "components": [],
            "dependence_matrix": {},
            "governs_readiness": False,
            "loss": None,
            "measured_component_count": 0,
            "measured_pair_count": 0,
            "method": "UNAVAILABLE_LEGACY_ACCEPTED_GENERATION",
            "prior_pair_count": expected_cells,
            "prior_component_count": 0,
            "status": "UNAVAILABLE_LEGACY_ACCEPTED_GENERATION",
        }
    else:
        central_case = _object(raw_central_case, "central_case")
        if central_case.get("governs_readiness") is not False:
            raise ControlPanelValidationError("central case must not govern readiness")
        central_components = _objects(
            central_case.get("components"), "central_case.components"
        )
        central_component_ids = {
            _text(item.get("series_id"), "central-case component series_id")
            for item in central_components
        }
        if central_component_ids != set(series_ids):
            raise ControlPanelValidationError(
                "central-case components differ from the current strategy boundary"
            )
        _finite_float(central_case.get("loss"), "central_case.loss")
        measured_pairs = central_case.get("measured_pair_count")
        prior_pairs = central_case.get(
            "prior_pair_count", central_case.get("missing_pair_count")
        )
        if (
            not isinstance(measured_pairs, int)
            or isinstance(measured_pairs, bool)
            or not isinstance(prior_pairs, int)
            or isinstance(prior_pairs, bool)
            or measured_pairs + prior_pairs != expected_cells
        ):
            raise ControlPanelValidationError("central-case pair coverage differs")
    if economic_contract_snapshot is None:
        cluster_contract, _cluster_content, cluster_contract_sha256 = _json_snapshot(
            ECONOMIC_DRIVER_CLUSTERS, "economic driver cluster contract"
        )
    else:
        cluster_contract = _json_from_bytes(
            economic_contract_snapshot, "economic driver cluster contract"
        )
        cluster_contract_sha256 = hashlib.sha256(economic_contract_snapshot).hexdigest()
    if cluster_contract.get("schema_version") != 1:
        raise ControlPanelValidationError("economic driver cluster schema differs")
    cluster_definitions = _objects(
        cluster_contract.get("clusters"), "economic driver clusters"
    )
    declared_members: list[str] = []
    for cluster in cluster_definitions:
        members = cluster.get("members")
        if not isinstance(members, list) or not members:
            raise ControlPanelValidationError("economic driver cluster members differ")
        declared_members.extend(
            _text(member, "economic driver cluster member") for member in members
        )
    if len(declared_members) != len(set(declared_members)) or not set(
        series_ids
    ) <= set(declared_members):
        raise ControlPanelValidationError(
            "economic driver clusters differ from the current strategy boundary"
        )
    component_index = {
        _text(
            component.get("series_id"), "correlation-one component series_id"
        ): component
        for component in _objects(
            correlation_one.get("components"), "correlation_one.components"
        )
    }
    if set(component_index) != set(series_ids):
        raise ControlPanelValidationError(
            "correlation-one components differ from economic driver clusters"
        )
    systematic_limit = _finite_float(
        cluster_contract.get("systematic_loss_limit"), "systematic_loss_limit"
    )
    correlation_one_loss = _finite_float(
        correlation_one.get("loss"), "correlation_one.loss"
    )
    central_case_loss = (
        None
        if central_case.get("loss") is None
        else _finite_float(central_case.get("loss"), "central_case.loss")
    )
    economic_driver_clusters: list[dict[str, Any]] = []
    for cluster in cluster_definitions:
        members = [
            _text(member, "economic driver cluster member")
            for member in cluster["members"]
            if member in series_ids
        ]
        if not members:
            continue
        pair_evidence = [
            {
                "decision_correlation": pair.get("decision_correlation"),
                "decision_status": pair.get("decision_status"),
                "left": pair.get("left"),
                "observations": pair.get("observations"),
                "observed_correlation": pair.get("observed_correlation"),
                "right": pair.get("right"),
            }
            for pair in blends
            if pair.get("left") in members and pair.get("right") in members
        ]
        observed_values = [
            _finite_float(pair["observed_correlation"], "observed_correlation")
            for pair in pair_evidence
            if pair.get("observed_correlation") is not None
        ]
        allocation_weight = sum(
            _finite_float(
                component_index[member].get("allocation_weight"),
                "component.allocation_weight",
            )
            for member in members
        )
        loss_contribution = sum(
            _finite_float(
                component_index[member].get("loss_contribution"),
                "component.loss_contribution",
            )
            for member in members
        )
        economic_driver_clusters.append(
            {
                "allocation_weight": _number_text(allocation_weight),
                "budget_status": (
                    "BREACHES_FULL_SYSTEMATIC_15_PERCENT_LOSS_LIMIT"
                    if loss_contribution > systematic_limit
                    else "WITHIN_FULL_SYSTEMATIC_15_PERCENT_LOSS_LIMIT"
                ),
                "cluster_id": _text(cluster.get("cluster_id"), "cluster_id"),
                "label": _text(cluster.get("label"), "cluster.label"),
                "loss_contribution": _number_text(loss_contribution),
                "members": members,
                "observed_pair_correlation_maximum": (
                    None if not observed_values else _number_text(max(observed_values))
                ),
                "observed_pair_correlation_minimum": (
                    None if not observed_values else _number_text(min(observed_values))
                ),
                "pair_evidence": pair_evidence,
                "rationale": _text(cluster.get("rationale"), "cluster.rationale"),
            }
        )
    cluster_budget = {
        "budget_rule": _text(cluster_contract.get("budget_rule"), "budget_rule"),
        "clusters": economic_driver_clusters,
        "contract_sha256": cluster_contract_sha256,
        "central_case_loss": (
            None if central_case_loss is None else _number_text(central_case_loss)
        ),
        "correlation_one_loss": _number_text(correlation_one_loss),
        "governing_loss_measure": "CORRELATION_ONE_CONSERVATIVE_BOUND",
        "historical_audit_reference": _object(
            cluster_contract.get("historical_audit_reference"),
            "historical_audit_reference",
        ),
        "limit_breach": _number_text(correlation_one_loss - systematic_limit),
        "status": (
            "BREACHES_SYSTEMATIC_LOSS_LIMIT"
            if correlation_one_loss > systematic_limit
            else "WITHIN_SYSTEMATIC_LOSS_LIMIT"
        ),
        "systematic_loss_limit": _number_text(systematic_limit),
    }
    raw_composition = dependence.get("series_composition")
    if raw_composition is None:
        series_composition: dict[str, dict[str, Any]] = {}
    else:
        composition = _object(raw_composition, "dependence.series_composition")
        if set(composition) != set(series_ids):
            raise ControlPanelValidationError(
                "dependence series composition differs from the current strategy boundary"
            )
        series_composition = {
            series_id: _object(
                composition[series_id],
                f"dependence.series_composition.{series_id}",
            )
            for series_id in series_ids
        }
    return {
        "analytics_hash": _text(
            correlation.get("analytics_hash"), "correlation.analytics_hash"
        ),
        "as_of_date": _text(correlation.get("as_of_date"), "correlation.as_of_date"),
        "available_pair_count": sum(
            item.get("status") == "AVAILABLE" for item in observed
        ),
        "cells": cells,
        "central_case": central_case,
        "correlation_one": correlation_one,
        "economic_driver_cluster_budget": cluster_budget,
        "evidence_mode": evidence_mode,
        "generation_id": identity["generation_id"],
        "drawdown_matrices": drawdown_matrices,
        "horizon_matrices": horizon_matrices,
        "matrices": matrices,
        "pair_count": expected_cells,
        "series_ids": series_ids,
        "series_composition": series_composition,
        "source_sha256": identity["sha256"],
        # The components' allocation weights are the owner-approved
        # PORT-DEPENDENCE-1 policy weights, not the cutoff capital weights.
        "weight_authority": _dependence_weight_authority(),
        "weight_basis": WEIGHT_BASIS_POLICY,
    }


def _report_return_window(
    report_strategy_returns: Sequence[Mapping[str, Any]],
    *,
    strategy_id: str,
    evidence_layer: str,
    observed_start: str,
    observed_end: str,
) -> dict[str, Any]:
    """Independently hash the report's normalized values for one analysis window."""

    rows: list[dict[str, str]] = []
    for raw in report_strategy_returns:
        row = _object(raw, "report strategy return")
        if (
            row.get("strategy_id") != strategy_id
            or row.get("evidence_layer") != evidence_layer
        ):
            continue
        raw_day = _text(row.get("date"), "report strategy return date")
        try:
            day = date.fromisoformat(raw_day).isoformat()
        except ValueError as exc:
            raise ControlPanelValidationError(
                "report strategy return date is invalid"
            ) from exc
        if day != raw_day:
            raise ControlPanelValidationError(
                "report strategy return date is not canonical"
            )
        if day < observed_start or day > observed_end:
            continue
        raw_return = _text(row.get("return"), "report strategy return value")
        try:
            return_value = Decimal(raw_return)
        except InvalidOperation as exc:
            raise ControlPanelValidationError(
                "report strategy return value is invalid"
            ) from exc
        if not return_value.is_finite():
            raise ControlPanelValidationError(
                "report strategy return value is not finite"
            )
        rows.append({"date": day, "return": decimal_text(return_value)})
    rows.sort(key=lambda item: item["date"])
    dates = [row["date"] for row in rows]
    if not rows or len(dates) != len(set(dates)):
        raise ControlPanelValidationError(
            f"report canonical-series window is absent or duplicated: {strategy_id}"
        )
    content = (
        json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")
    return {
        "canonical_live_series_sha256": hashlib.sha256(content).hexdigest(),
        "observed_end": rows[-1]["date"],
        "observed_start": rows[0]["date"],
        "return_observations": len(rows),
    }


def _canonical_strategy_series_contracts(
    evidence_by_strategy: Mapping[str, Mapping[str, Any]],
    strategy_names: Mapping[str, str],
    factor_analysis: Mapping[str, Any],
    dependence_analysis: Mapping[str, Any],
    weekly_returns: Mapping[str, Any],
    report_strategy_returns: Sequence[Mapping[str, Any]],
    dependence_strategy_names: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Prove that every analytical view consumed the canonical live path."""

    if (
        not evidence_by_strategy
        or weekly_returns.get("generation_id") is None
        or not dependence_analysis.get("series_composition")
    ):
        return []
    composition = _object(
        dependence_analysis.get("series_composition"),
        "dependence series composition",
    )
    factor_by_name = {
        _text(row.get("series_id"), "factor series_id"): row
        for row in _objects(factor_analysis.get("series"), "factor series")
    }
    weekly_by_name = {
        _text(row.get("display_name"), "weekly display_name"): row
        for row in _objects(weekly_returns.get("series"), "weekly series")
        if row.get("primary_group") == "STRATEGY"
    }
    contracts: list[dict[str, Any]] = []
    for strategy_id, evidence in sorted(evidence_by_strategy.items()):
        series_id = strategy_names[strategy_id]
        if (
            dependence_strategy_names is not None
            and series_id not in dependence_strategy_names
        ):
            continue
        layer = _text(evidence.get("evidence_layer"), "canonical evidence layer")
        selected_key = "account" if layer == "ACCOUNT_LIVE" else "provider"
        selected = _object(
            evidence.get(selected_key), f"canonical {series_id} {selected_key}"
        )
        observation_count = selected.get("return_observations")
        if (
            isinstance(observation_count, bool)
            or not isinstance(observation_count, int)
            or observation_count <= 0
        ):
            raise ControlPanelValidationError(
                f"canonical strategy series is empty: {series_id}"
            )
        evidence_version = evidence.get("strategy_version")
        dependence = _object(
            composition.get(series_id), f"dependence series composition {series_id}"
        )
        dependence_version = dependence.get("strategy_version")
        live_version_ref = dependence.get("live_version_ref")
        if not isinstance(evidence_version, str) or not evidence_version.strip():
            raise ControlPanelValidationError(
                f"evidence strategy version is absent: {series_id}"
            )
        if not isinstance(dependence_version, str) or not dependence_version.strip():
            raise ControlPanelValidationError(
                f"dependence strategy version is absent: {series_id}"
            )
        strategy_version = _text(
            dependence_version, f"dependence strategy version {series_id}"
        )
        # The evidence row attests the accepted live path; the dependence
        # composition's full-series strategy_version may additionally bind a
        # declared versioned history (or a governance disposition sentinel).
        # Parity is therefore checked against the dependence live_version_ref,
        # which is hash-bound to the same live rows as the evidence version.
        if not isinstance(live_version_ref, str) or not live_version_ref.strip():
            raise ControlPanelValidationError(
                f"dependence live version is absent: {series_id}"
            )
        dependence_observed_end = _text(
            dependence.get("observed_end"), f"dependence observed_end {series_id}"
        )
        dependence_observed_start = _text(
            dependence.get("observed_start"), f"dependence observed_start {series_id}"
        )
        dependence_observations = dependence.get("live_observations")
        if (
            isinstance(dependence_observations, bool)
            or not isinstance(dependence_observations, int)
            or dependence_observations <= 0
        ):
            raise ControlPanelValidationError(
                f"dependence live observations are invalid: {series_id}"
            )
        dependence_series_sha256 = _text(
            dependence.get("canonical_live_series_sha256"),
            f"dependence canonical series sha256 {series_id}",
        )
        report_window = _report_return_window(
            report_strategy_returns,
            strategy_id=strategy_id,
            evidence_layer=layer,
            observed_start=dependence_observed_start,
            observed_end=dependence_observed_end,
        )
        if report_window != {
            "canonical_live_series_sha256": dependence_series_sha256,
            "observed_end": dependence_observed_end,
            "observed_start": dependence_observed_start,
            "return_observations": dependence_observations,
        }:
            raise ControlPanelValidationError(
                f"report/dependence canonical-series value parity failed: {series_id}"
            )
        expected = {
            "evidence_layer": layer,
            "generation_id": _text(
                evidence.get("generation_id"), "canonical generation_id"
            ),
            "observed_end": _text(
                selected.get("observed_end"), "canonical observed_end"
            ),
            "observed_start": _text(
                selected.get("observed_start"), "canonical observed_start"
            ),
            "return_observations": observation_count,
            "series_id": series_id,
            "strategy_id": strategy_id,
            "live_version_ref": live_version_ref,
            "report_catalog_version_ref": evidence_version,
            "strategy_version": strategy_version,
        }
        dependence_observed = {
            "evidence_layer": dependence.get("live_evidence_layer"),
            "observed_start": dependence.get("observed_start"),
        }
        if dependence_observed != {
            key: expected[key] for key in ("evidence_layer", "observed_start")
        }:
            raise ControlPanelValidationError(
                f"dependence canonical-series origin parity failed: {series_id}"
            )
        # The evidence row may extend beyond the current dependence window, but
        # the dependence window can never extend beyond the evidenced path.
        evidence_observed_end = _text(
            expected["observed_end"], f"evidence observed_end {series_id}"
        )
        if dependence_observed_end > evidence_observed_end:
            raise ControlPanelValidationError(
                f"dependence canonical series extends beyond the evidenced live path: {series_id}"
            )

        factor = factor_by_name.get(series_id)
        if factor is None or not isinstance(factor.get("coverage"), dict):
            raise ControlPanelValidationError(
                f"factor canonical-series coverage is absent: {series_id}"
            )
        factor_coverage = _object(factor["coverage"], f"factor coverage {series_id}")
        factor_series_sha256 = _text(
            factor_coverage.get("canonical_live_series_sha256"),
            f"factor canonical series sha256 {series_id}",
        )
        factor_observed_end = _text(
            factor_coverage.get(
                "canonical_live_observed_end",
                factor_coverage.get("target_observed_end"),
            ),
            f"factor canonical observed_end {series_id}",
        )
        factor_observed_start = _text(
            factor_coverage.get(
                "canonical_live_observed_start",
                factor_coverage.get("target_observed_start"),
            ),
            f"factor canonical observed_start {series_id}",
        )
        factor_observations = factor_coverage.get(
            "canonical_live_observations",
            factor_coverage.get("target_observations"),
        )
        if (
            isinstance(factor_observations, bool)
            or not isinstance(factor_observations, int)
            or factor_observations <= 0
            or factor_observed_start < dependence_observed_start
            or factor_observed_end > dependence_observed_end
        ):
            raise ControlPanelValidationError(
                f"factor canonical-series window differs: {series_id}"
            )
        factor_report_window = _report_return_window(
            report_strategy_returns,
            strategy_id=strategy_id,
            evidence_layer=layer,
            observed_start=factor_observed_start,
            observed_end=factor_observed_end,
        )
        if factor_report_window != {
            "canonical_live_series_sha256": factor_series_sha256,
            "observed_end": factor_observed_end,
            "observed_start": factor_observed_start,
            "return_observations": factor_observations,
        }:
            raise ControlPanelValidationError(
                f"report/factor canonical-series value parity failed: {series_id}"
            )

        weekly = weekly_by_name.get(series_id)
        if weekly is None:
            raise ControlPanelValidationError(
                f"weekly canonical-series coverage is absent: {series_id}"
            )
        mode_coverage = _object(
            _object(weekly.get("mode_coverage"), "weekly mode coverage").get(
                "LIVE_ONLY"
            ),
            f"weekly LIVE_ONLY coverage {series_id}",
        )
        observation_meta = _object(
            _object(
                weekly.get("observation_meta_by_mode"),
                "weekly observation metadata",
            ).get("LIVE_ONLY"),
            f"weekly LIVE_ONLY observations {series_id}",
        )
        weekly_bounds = [
            value
            for value in observation_meta.values()
            if isinstance(value, list) and len(value) >= 3
        ]
        if not weekly_bounds:
            raise ControlPanelValidationError(
                f"weekly canonical-series observations are absent: {series_id}"
            )
        weekly_observed = {
            "canonical_live_series_sha256": mode_coverage.get(
                "canonical_live_series_sha256"
            ),
            "observed_end": max(str(value[2]) for value in weekly_bounds),
            "observed_start": min(str(value[1]) for value in weekly_bounds),
            "strategy_version": mode_coverage.get("strategy_version"),
        }
        if weekly_observed != {
            "canonical_live_series_sha256": dependence_series_sha256,
            "observed_end": dependence_observed_end,
            "observed_start": dependence_observed["observed_start"],
            "strategy_version": strategy_version,
        }:
            raise ControlPanelValidationError(
                f"weekly canonical-series parity failed: {series_id}"
            )
        contracts.append(
            {
                **expected,
                "canonical_live_series_sha256": dependence_series_sha256,
                "factor_window_observed_end": factor_observed_end,
                "factor_window_observed_start": factor_observed_start,
                "factor_window_return_observations": factor_observations,
                "factor_window_series_sha256": factor_series_sha256,
            }
        )
    return contracts


def _optional_float(value: object, field_name: str) -> float | None:
    return None if value is None else _finite_float(value, field_name)


def _quantile(values: Sequence[float], probability: float) -> float:
    if not values:
        raise ControlPanelValidationError("quantile input is empty")
    if probability < 0 or probability > 1:
        raise ControlPanelValidationError("quantile probability is outside [0, 1]")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _mean_or_none(values: Sequence[float]) -> str | None:
    return _number_text(sum(values) / len(values)) if values else None


def _weekly_values(
    row: Mapping[str, Any],
    *,
    evidence_mode: str | None = None,
) -> dict[str, float]:
    if evidence_mode is None:
        raw_values = _object(row.get("values"), "weekly values")
        raw_metadata = _object(row.get("observation_meta"), "weekly metadata")
    else:
        modes = _object(row.get("values_by_mode"), "weekly values_by_mode")
        metadata_modes = _object(
            row.get("observation_meta_by_mode"),
            "weekly observation_meta_by_mode",
        )
        raw = modes.get(evidence_mode)
        raw_metadata_mode = metadata_modes.get(evidence_mode)
        if raw is None or raw_metadata_mode is None:
            return {}
        raw_values = _object(raw, f"weekly values_by_mode.{evidence_mode}")
        raw_metadata = _object(
            raw_metadata_mode,
            f"weekly observation_meta_by_mode.{evidence_mode}",
        )
    accepted: dict[str, float] = {}
    for week, value in raw_values.items():
        if value is None:
            continue
        try:
            date.fromisoformat(week)
        except ValueError as exc:
            raise ControlPanelValidationError(
                f"weekly return has an invalid week: {week}"
            ) from exc
        metadata = raw_metadata.get(week)
        if (
            not isinstance(metadata, list)
            or len(metadata) < 4
            or metadata[3] != "COMPLETE_WITHIN_ACCEPTED_CALENDAR"
        ):
            continue
        accepted[week] = _finite_float(value, f"weekly return {week}")
    return accepted


def _factor_row_for_strategy(
    decomposition_lenses: Mapping[str, Any],
    evidence_mode: str,
    strategy_name: str,
    lens_id: str = "ECONOMIC_FACTOR",
) -> dict[str, Any] | None:
    """The strategy's own usable fit in this lens, never the first row by position.

    Every source key of a lens lists every strategy, and a strategy outside a
    source's method scope has an UNAVAILABLE placeholder there.  The row is the
    strategy's AVAILABLE row, else its DIAGNOSTIC row, preferring the lens's
    default source when more than one source fits.  A strategy no source fits has
    no row: its statistics are absent, and the series rows keep their reasons.
    """

    modes = _object(decomposition_lenses.get("modes"), "decomposition lens modes")
    lenses_value = modes.get(evidence_mode)
    if lenses_value is None:
        return None
    lenses = _objects(lenses_value, f"decomposition lenses {evidence_mode}")
    lens = next((row for row in lenses if row.get("lens_id") == lens_id), None)
    if lens is None:
        return None
    rows = [
        row
        for row in _objects(lens.get("series"), f"{lens_id} series")
        if row.get("series_id") == strategy_name
    ]
    default_source_key = lens.get("default_source_key")
    for status_prefix in ("AVAILABLE", "DIAGNOSTIC"):
        usable = [
            row for row in rows if str(row.get("status", "")).startswith(status_prefix)
        ]
        preferred = next(
            (row for row in usable if row.get("source_key") == default_source_key),
            None,
        )
        if preferred is not None:
            return preferred
        if usable:
            return usable[0]
    return None


LONG_HISTORY_MEMBERSHIP_RULE: Final = (
    "H2_ACCEPTED_HISTORY_AND_ELIGIBLE_LIVE_PLUS_BACKTEST"
)


def _version_matched_history_observations(
    strategy: Mapping[str, Any], hybrid_factor: Mapping[str, Any] | None
) -> int | None:
    """Matched backtest observations under the H2 hybrid-gate predicate.

    A strategy is a member when its accepted history is admitted for economic
    inference and the report marks live plus backtest eligible; the count is the
    strategy's own `history_observations`.  Whether a decomposition lens could fit
    that history is a separate statistic and never sets membership.  Returns None
    for a strategy that is not a member.  A report without the history contract
    (`history_compatibility_status` absent) keeps the older evidence: the
    backtest observations its hybrid lens fit consumed.
    """

    strategy_name = _text(strategy.get("strategy_name"), "history strategy_name")
    status = strategy.get("history_compatibility_status")
    if status is None:
        composition = (
            _object(
                hybrid_factor.get("series_composition", {}),
                f"hybrid factor composition {strategy_name}",
            )
            if hybrid_factor is not None
            else {}
        )
        used = int(
            _finite_float(
                composition.get("backtest_observations_used", 0),
                f"matched backtest observations {strategy_name}",
            )
        )
        return used if used > 0 else None
    if (
        status != "ACCEPTED_FOR_ECONOMIC_INFERENCE"
        or strategy.get("live_plus_backtest_status") != "ELIGIBLE_ACCEPTED_HISTORY"
    ):
        return None
    observations = strategy.get("history_observations")
    if observations is None:
        return 0
    return int(_finite_float(observations, f"history observations {strategy_name}"))


STALE_IMPLEMENTATION_STATUS: Final = "NOT_READY_STALE_CONTRACT_LEDGER_VINTAGE"
NO_RETURN_RECONCILIATION_STATUS: Final = "NOT_READY_NO_RETURN_RECONCILIATION"
_STALE_IMPLEMENTATION_KEYS: Final = frozenset(
    {
        "status",
        "strategy_id",
        "contract_kind",
        "contract_id",
        "contract_sha256",
        "contract_as_of_date",
        "window_start",
        "window_end",
        "governed_account_return",
        "governed_account_basis",
        "governed_observations",
        "contract_provider_ledger_sha256",
        "governed_provider_ledger_sha256",
        "superseded_contract_account_return",
        "superseded_contract_account_basis",
        "reason",
        "repair_ref",
    }
)
_IMPLEMENTATION_REFERENCE_KEYS: Final = (
    ("reconciliation", "implementation_reconciliation_contract"),
    ("return_attribution", "implementation_return_attribution_contract"),
)


def _is_stale_implementation(value: object) -> bool:
    return isinstance(value, Mapping) and str(value.get("status", "")).startswith(
        "NOT_READY_STALE"
    )


def _validate_stale_implementation_object(
    value: object, *, kind: str, strategy_id: str
) -> dict[str, Any]:
    """The report's closed NOT_READY object for a contract built from another ledger.

    It carries the governed account return with its basis and the reason, and no
    model return, gap, risk statistic or waterfall.  The contract's own superseded
    figure stays beside its explicit basis label and is never read as a return.
    """

    row = _object(value, f"stale implementation {kind} {strategy_id}")
    if set(row) != _STALE_IMPLEMENTATION_KEYS:
        raise ControlPanelValidationError(
            f"stale implementation object keys differ: {strategy_id}"
        )
    pins = []
    for field in (
        "contract_provider_ledger_sha256",
        "governed_provider_ledger_sha256",
    ):
        pin = row[field]
        if not isinstance(pin, str) or re.fullmatch(r"[0-9a-f]{64}", pin) is None:
            raise ControlPanelValidationError(
                f"stale implementation ledger identity is invalid: {strategy_id}"
            )
        pins.append(pin)
    if (
        row["status"] != STALE_IMPLEMENTATION_STATUS
        or row["contract_kind"] != kind
        or row["strategy_id"] != strategy_id
        or row["governed_account_basis"] != "BASELINE_ACCOUNT_LIVE"
        or row["superseded_contract_account_basis"]
        != "SUPERSEDED_LEDGER_VINTAGE_NOT_AN_ACCOUNT_RETURN"
        or row["repair_ref"] != "N66-R"
        or pins[0] == pins[1]
        or type(row["governed_observations"]) is not int
        or row["governed_observations"] < 1
        or not isinstance(row["reason"], str)
        or not row["reason"]
    ):
        raise ControlPanelValidationError(
            f"stale implementation object is not a closed NOT_READY row: {strategy_id}"
        )
    for field in ("governed_account_return", "superseded_contract_account_return"):
        _finite_float(row[field], f"stale implementation {field}")
    for field in ("contract_as_of_date", "window_start", "window_end"):
        _panel_date(row[field], f"stale implementation {field}")
    return dict(row)


def _implementation_contract_context(
    strategy_id: str,
    present: Mapping[str, bool],
    references: Mapping[str, Mapping[str, Any] | None],
) -> dict[str, Any]:
    """Which contract dates and flags label this strategy's implementation rows."""

    context: dict[str, Any] = {"provider_return_policy": None}
    for kind, key in _IMPLEMENTATION_REFERENCE_KEYS:
        reference = references.get(key)
        context[kind] = None
        if not isinstance(reference, Mapping) or not present.get(kind):
            continue
        context[kind] = {
            "as_of_date": reference.get("as_of_date"),
            "stale": strategy_id in (reference.get("stale_strategy_ids") or []),
            "pre_policy": strategy_id
            in (reference.get("pre_policy_strategy_ids") or []),
        }
        policy = reference.get("provider_return_policy")
        if isinstance(policy, Mapping) and context["provider_return_policy"] is None:
            context["provider_return_policy"] = {
                "policy_id": policy.get("policy_id"),
                "effective_start": policy.get("effective_start"),
            }
    return context


def _model_account_gap_underwriting(
    strategy: Mapping[str, Any], gap_policy: Mapping[str, Any]
) -> dict[str, Any]:
    strategy_name = _text(strategy.get("strategy_name"), "gap strategy_name")
    implementation = strategy.get("implementation_reconciliation")
    if isinstance(implementation, Mapping):
        metrics = implementation.get("return_reconciliation")
        if (
            isinstance(metrics, Mapping)
            and metrics.get("status") == "UNAVAILABLE_POLICY_NOT_ADMITTED"
        ):
            import portfolio_report as reporter

            reporter._validate_policy_unavailable_comparison(
                metrics, strategy_id=str(strategy.get("strategy_id"))
            )
            return {
                "status": "UNAVAILABLE_POLICY_NOT_ADMITTED",
                "comparison_status": "UNAVAILABLE_POLICY_NOT_ADMITTED",
                "observed_start": None,
                "observed_end": None,
                "observations": 0,
                "reference_cumulative_return": None,
                "account_cumulative_return": None,
                "total_gap": None,
                "causally_bound_components": [],
                "unresolved_residual": None,
                "reconciliation_error": None,
                "signature": "UNAVAILABLE_POLICY_NOT_ADMITTED",
                "beta": None,
                "correlation": None,
                "tracking_error_annualized": None,
                "unresolved_causes": ["PROVIDER_RETURN_POLICY_NOT_ADMITTED"],
                "cost_boundary": gap_policy.get("subscription_cost_rule"),
                "policy_evidence": dict(metrics["policy_evidence"]),
                "account_live_return_summary": implementation.get(
                    "account_live_return_summary"
                ),
            }
    comparison_status = str(strategy.get("provider_comparison_status") or "UNAVAILABLE")
    available = comparison_status.startswith("DESCRIPTIVE_EXACT_DATE")
    total_gap = (
        _optional_float(
            strategy.get("provider_cumulative_gap"),
            f"provider cumulative gap {strategy_name}",
        )
        if available
        else None
    )
    beta = (
        _optional_float(strategy.get("provider_beta"), f"provider beta {strategy_name}")
        if available
        else None
    )
    correlation = (
        _optional_float(
            strategy.get("provider_correlation"),
            f"provider correlation {strategy_name}",
        )
        if available
        else None
    )
    tracking_error = (
        _optional_float(
            strategy.get("provider_tracking_error_annualized"),
            f"provider tracking error {strategy_name}",
        )
        if available
        else None
    )
    if not available:
        signature = "UNAVAILABLE_NO_ACCEPTED_MODEL_OR_PROVIDER_COMPARISON"
    elif (
        correlation is not None
        and beta is not None
        and tracking_error is not None
        and correlation
        >= _finite_float(
            gap_policy.get("path_aligned_minimum_correlation"),
            "path_aligned_minimum_correlation",
        )
        and _finite_float(
            gap_policy.get("path_aligned_beta_minimum"),
            "path_aligned_beta_minimum",
        )
        <= beta
        <= _finite_float(
            gap_policy.get("path_aligned_beta_maximum"),
            "path_aligned_beta_maximum",
        )
        and tracking_error
        <= _finite_float(
            gap_policy.get("path_aligned_maximum_tracking_error_annualized"),
            "path_aligned_maximum_tracking_error_annualized",
        )
    ):
        signature = "PATH_ALIGNED_PERSISTENT_LEVEL_DIFFERENCE"
    elif correlation is not None and correlation >= _finite_float(
        gap_policy.get("partial_path_minimum_correlation"),
        "partial_path_minimum_correlation",
    ):
        signature = "PARTIAL_PATH_DIVERGENCE"
    else:
        signature = "MATERIAL_PATH_DIVERGENCE"
    unresolved_causes: list[str] = []
    if available:
        if (
            strategy.get("provider_comparison_version_compatibility")
            != "PROVEN_COMPATIBLE"
        ):
            unresolved_causes.append("REFERENCE_VERSION_COMPATIBILITY_NOT_PROVEN")
        if strategy.get("cash_drag_status") != "AVAILABLE":
            unresolved_causes.append("COMPLETE_DAILY_ACCOUNT_CASH_PATH_UNAVAILABLE")
        if strategy.get("fees_status") != "AVAILABLE_COMPLETE":
            unresolved_causes.append("ACCOUNT_BOUND_FEE_PATH_INCOMPLETE")
        unresolved_causes.extend(
            [
                "ORDER_TIMING_AND_FILL_ATTRIBUTION_UNAVAILABLE",
                "SLIPPAGE_AND_TRADE_LEVEL_CAUSAL_ATTRIBUTION_UNAVAILABLE",
            ]
        )
    # No current fact is causally bound to the exact-date gap. The residual is
    # therefore the complete total, rather than a fabricated component split.
    unresolved_residual = total_gap
    return {
        "status": (
            "QUANTIFIED_TOTAL_CAUSAL_COMPONENTS_UNRESOLVED"
            if available
            else "UNAVAILABLE_NO_ACCEPTED_MODEL_OR_PROVIDER_COMPARISON"
        ),
        "comparison_status": comparison_status,
        "observed_start": strategy.get("provider_comparison_observed_start"),
        "observed_end": strategy.get("provider_comparison_observed_end"),
        "observations": strategy.get("provider_comparison_observations"),
        "reference_cumulative_return": strategy.get(
            "provider_reference_cumulative_return"
        ),
        "account_cumulative_return": strategy.get("provider_account_cumulative_return"),
        "total_gap": _number_text(total_gap) if total_gap is not None else None,
        "causally_bound_components": [],
        "unresolved_residual": (
            _number_text(unresolved_residual)
            if unresolved_residual is not None
            else None
        ),
        "reconciliation_error": (
            _number_text(total_gap - unresolved_residual)
            if total_gap is not None and unresolved_residual is not None
            else None
        ),
        "signature": signature,
        "beta": strategy.get("provider_beta"),
        "correlation": strategy.get("provider_correlation"),
        "tracking_error_annualized": strategy.get("provider_tracking_error_annualized"),
        "unresolved_causes": unresolved_causes,
        "cost_boundary": gap_policy.get("subscription_cost_rule"),
    }


def _regime_underwriting(
    strategy: Mapping[str, Any],
    weekly_rows: Mapping[str, Mapping[str, Any]],
    regime_policy: Mapping[str, Any],
    *,
    matched_backtest_observations: int,
) -> dict[str, Any]:
    strategy_name = _text(strategy.get("strategy_name"), "regime strategy_name")
    benchmark_specs = _object(
        regime_policy.get("benchmark_series"), "regime benchmark series"
    )
    benchmark_id = strategy.get("benchmark_id")
    specification = (
        _object(benchmark_specs[benchmark_id], f"benchmark {benchmark_id}")
        if isinstance(benchmark_id, str) and benchmark_id in benchmark_specs
        else None
    )
    benchmark_map: dict[str, float] = {}
    accepted_specification: dict[str, Any] | None = None
    if specification is not None:
        components_value = specification.get("components")
        if not isinstance(components_value, list) or not components_value:
            raise ControlPanelValidationError(
                f"benchmark specification lacks components: {benchmark_id}"
            )
        component_ids = [
            _text(value, f"benchmark component {benchmark_id}")
            for value in components_value
        ]
        component_maps: list[dict[str, float]] = []
        for component_id in component_ids:
            component = weekly_rows.get(component_id)
            if component is None:
                component_maps = []
                break
            component_maps.append(_weekly_values(component))
        if component_maps:
            common_weeks = set(component_maps[0])
            for values in component_maps[1:]:
                common_weeks &= set(values)
            benchmark_map = {
                week: sum(values[week] for values in component_maps)
                / len(component_maps)
                for week in sorted(common_weeks)
            }
        accepted_specification = {
            **specification,
            "benchmark_id": benchmark_id,
            "components": component_ids,
        }

    strategy_weekly = weekly_rows.get(f"strategy:{strategy_name}")
    live_values = (
        _weekly_values(strategy_weekly, evidence_mode="LIVE_ONLY")
        if strategy_weekly is not None
        else {}
    )
    hybrid_values = (
        _weekly_values(strategy_weekly, evidence_mode="LIVE_PLUS_BACKTEST")
        if strategy_weekly is not None and matched_backtest_observations > 0
        else {}
    )
    live_start_value = strategy.get("account_live_observed_start")
    live_start = (
        date.fromisoformat(str(live_start_value))
        if isinstance(live_start_value, str) and live_start_value
        else None
    )
    live_week = (
        (live_start - timedelta(days=live_start.weekday())).isoformat()
        if live_start is not None
        else None
    )
    threshold_history = (
        [
            value
            for week, value in benchmark_map.items()
            if live_week and week < live_week
        ]
        if live_week
        else []
    )
    minimum_threshold_weeks = int(
        _finite_float(
            regime_policy.get("minimum_threshold_history_weeks"),
            "minimum_threshold_history_weeks",
        )
    )
    minimum_prehistory_weeks = int(
        _finite_float(
            regime_policy.get("minimum_matched_prehistory_regime_weeks"),
            "minimum_matched_prehistory_regime_weeks",
        )
    )
    minimum_live_weeks = int(
        _finite_float(
            regime_policy.get("minimum_live_regime_weeks_for_distribution_comparison"),
            "minimum_live_regime_weeks_for_distribution_comparison",
        )
    )
    threshold_status = (
        "AVAILABLE_PRELIVE_BENCHMARK_HISTORY"
        if len(threshold_history) >= minimum_threshold_weeks
        else "UNAVAILABLE_INSUFFICIENT_PRELIVE_BENCHMARK_HISTORY"
    )
    lower_tail_threshold: float | None = None
    high_absolute_threshold: float | None = None
    regime_rows: list[dict[str, Any]] = []
    if threshold_status == "AVAILABLE_PRELIVE_BENCHMARK_HISTORY":
        lower_tail_threshold = _quantile(
            threshold_history,
            _finite_float(regime_policy.get("lower_tail_quantile"), "lower tail"),
        )
        high_absolute_threshold = _quantile(
            [abs(value) for value in threshold_history],
            _finite_float(
                regime_policy.get("high_absolute_return_quantile"),
                "high absolute return quantile",
            ),
        )
        predicates = {
            "BENCHMARK_NEGATIVE": lambda value: value < 0,
            "BENCHMARK_LOWER_DECILE": lambda value: value <= lower_tail_threshold,
            "BENCHMARK_HIGH_ABSOLUTE_RETURN": lambda value: (
                abs(value) >= high_absolute_threshold
            ),
        }
        regimes_value = regime_policy.get("regimes")
        if not isinstance(regimes_value, list) or not regimes_value:
            raise ControlPanelValidationError("underwriting regimes are absent")
        for raw_regime_id in regimes_value:
            regime_id = _text(raw_regime_id, "underwriting regime")
            predicate = predicates.get(regime_id)
            if predicate is None:
                raise ControlPanelValidationError(
                    f"unsupported underwriting regime: {regime_id}"
                )
            live_observations = [
                (week, live_values[week], benchmark_map[week])
                for week in sorted(set(live_values) & set(benchmark_map))
                if live_week and week >= live_week and predicate(benchmark_map[week])
            ]
            prehistory_observations = [
                (week, hybrid_values[week], benchmark_map[week])
                for week in sorted(set(hybrid_values) & set(benchmark_map))
                if live_week and week < live_week and predicate(benchmark_map[week])
            ]
            prehistory_values = [item[1] for item in prehistory_observations]
            empirical_range = (
                (
                    _quantile(
                        prehistory_values,
                        _finite_float(
                            regime_policy.get("empirical_lower_quantile"),
                            "empirical lower quantile",
                        ),
                    ),
                    _quantile(
                        prehistory_values,
                        _finite_float(
                            regime_policy.get("empirical_upper_quantile"),
                            "empirical upper quantile",
                        ),
                    ),
                )
                if len(prehistory_values) >= minimum_prehistory_weeks
                else None
            )
            outside_count = (
                sum(
                    value < empirical_range[0] or value > empirical_range[1]
                    for _, value, _ in live_observations
                )
                if empirical_range is not None
                else None
            )
            if not live_observations:
                state = "UNAVAILABLE_NO_LIVE_REGIME_OBSERVATIONS"
            elif empirical_range is None:
                state = "EVENT_EVIDENCE_ONLY_NO_MATCHED_PREHISTORY_RANGE"
            elif len(live_observations) < minimum_live_weeks:
                state = "EVENT_EVIDENCE_ONLY_INSUFFICIENT_LIVE_SAMPLE"
            else:
                state = "AVAILABLE_EMPIRICAL_PREHISTORY_COMPARISON"
            latest = live_observations[-1] if live_observations else None
            regime_rows.append(
                {
                    "regime_id": regime_id,
                    "status": state,
                    "live_observations": len(live_observations),
                    "live_mean_return": _mean_or_none(
                        [item[1] for item in live_observations]
                    ),
                    "live_positive_fraction": (
                        _number_text(
                            sum(item[1] > 0 for item in live_observations)
                            / len(live_observations)
                        )
                        if live_observations
                        else None
                    ),
                    "benchmark_live_mean_return": _mean_or_none(
                        [item[2] for item in live_observations]
                    ),
                    "matched_prehistory_observations": len(prehistory_observations),
                    "matched_prehistory_mean_return": _mean_or_none(prehistory_values),
                    "empirical_p05": (
                        _number_text(empirical_range[0])
                        if empirical_range is not None
                        else None
                    ),
                    "empirical_p95": (
                        _number_text(empirical_range[1])
                        if empirical_range is not None
                        else None
                    ),
                    "outside_empirical_range_observations": outside_count,
                    "latest_week": latest[0] if latest is not None else None,
                    "latest_strategy_return": (
                        _number_text(latest[1]) if latest is not None else None
                    ),
                    "latest_benchmark_return": (
                        _number_text(latest[2]) if latest is not None else None
                    ),
                    "latest_event_range_status": (
                        (
                            "WITHIN_EMPIRICAL_PREHISTORY_90_PERCENT_RANGE"
                            if empirical_range[0] <= latest[1] <= empirical_range[1]
                            else "OUTSIDE_EMPIRICAL_PREHISTORY_90_PERCENT_RANGE_SINGLE_EVENT"
                        )
                        if latest is not None and empirical_range is not None
                        else "UNAVAILABLE_NO_LIVE_REGIME_EVENT"
                        if latest is None
                        else "UNAVAILABLE_NO_EMPIRICAL_PREHISTORY_RANGE"
                    ),
                }
            )
        if any(
            item["status"] == "AVAILABLE_EMPIRICAL_PREHISTORY_COMPARISON"
            for item in regime_rows
        ):
            status = "AVAILABLE_VERSION_MATCHED_PREHISTORY_COMPARISON"
        elif live_values:
            status = "DESCRIPTIVE_LIVE_EVENTS_ONLY"
        else:
            status = "UNAVAILABLE_NO_ACCOUNT_LIVE_WEEKLY_RETURNS"
    else:
        status = threshold_status
    return {
        "status": status,
        "threshold_status": threshold_status,
        "threshold_history_weeks": len(threshold_history),
        "benchmark": accepted_specification,
        "live_inception": live_start.isoformat() if live_start else None,
        "lower_tail_threshold": (
            _number_text(lower_tail_threshold)
            if lower_tail_threshold is not None
            else None
        ),
        "high_absolute_return_threshold": (
            _number_text(high_absolute_threshold)
            if high_absolute_threshold is not None
            else None
        ),
        "regimes": regime_rows,
        "interpretation": regime_policy.get("interpretation"),
    }


def _strategy_underwriting(
    strategies: Sequence[Mapping[str, Any]],
    decomposition_lenses: Mapping[str, Any],
    dependence: Mapping[str, Any],
    weekly_returns: Mapping[str, Any],
    portfolio_as_of: str,
    policy_snapshot: bytes | None = None,
    *,
    performance_lineage_available: bool = True,
    implementation_contracts: Mapping[str, Mapping[str, Any] | None] | None = None,
) -> tuple[dict[str, Any], str]:
    """Compose an advisory evidence ledger without creating new fact authority."""

    if policy_snapshot is None:
        policy, _policy_content, policy_hash = _json_snapshot(
            STRATEGY_UNDERWRITING_POLICY, "underwriting policy"
        )
    else:
        policy = _json_from_bytes(policy_snapshot, "underwriting policy")
        policy_hash = hashlib.sha256(policy_snapshot).hexdigest()
    if (
        policy.get("schema_version") != 1
        or policy.get("policy_id") != "PORT-UNDERWRITE-1"
    ):
        raise ControlPanelValidationError("underwriting policy identity is unsupported")
    rows: list[dict[str, Any]]
    if not performance_lineage_available:
        unavailable = "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH"
        rows = [
            {
                "strategy_id": _text(
                    strategy.get("strategy_id"), "underwriting strategy_id"
                ),
                "strategy_name": _text(
                    strategy.get("strategy_name"), "underwriting strategy_name"
                ),
                "evidence_layers": {"status": unavailable},
                "design_fit": {"status": unavailable},
                "long_history": {"status": unavailable},
                "model_account_gap": {
                    "status": unavailable,
                    "comparison_status": unavailable,
                    "total_gap": None,
                },
                "regime_conformance": {"status": unavailable},
                "portfolio_fit": {"status": unavailable},
                "advisory_disposition": {
                    "action": "RETAIN_MONITORING_SOURCE_LINEAGE_MISMATCH",
                    "reasons": [unavailable],
                    "drop_decision_status": "NO_AUTOMATIC_DROP_AUTHORITY",
                    "allocation_effect": "NONE",
                    "execution_effect": "NONE",
                },
            }
            for strategy in strategies
        ]
        if len(rows) != len(strategies) or {row["strategy_id"] for row in rows} != {
            str(row.get("strategy_id")) for row in strategies
        }:
            raise ControlPanelValidationError("underwriting strategy boundary differs")
        return (
            {
                "schema_version": 1,
                "policy_id": policy["policy_id"],
                "policy_sha256": policy_hash,
                "as_of_date": portfolio_as_of,
                "authority_boundary": _object(
                    policy.get("decision_boundary"), "underwriting decision boundary"
                ),
                "methodology": {"performance_lineage": unavailable},
                "counts": {
                    "strategies": len(rows),
                    "authoritative_design": 0,
                    "version_matched_long_history": 0,
                    "quantified_model_account_gap": 0,
                    "empirical_regime_comparison": 0,
                },
                "rows": rows,
            },
            policy_hash,
        )
    gap_policy = _object(policy.get("model_account_gap"), "model_account_gap policy")
    regime_policy = _object(policy.get("regime_conformance"), "regime policy")
    weekly_rows = {
        _text(row.get("series_id"), "weekly series_id"): row
        for row in _objects(weekly_returns.get("series"), "weekly series")
    }
    cluster_budget = _object(
        dependence.get("economic_driver_cluster_budget"),
        "economic_driver_cluster_budget",
    )
    clusters = _objects(cluster_budget.get("clusters"), "economic driver clusters")
    correlation_one = _object(dependence.get("correlation_one"), "correlation_one")
    components = {
        _text(row.get("series_id"), "correlation-one series_id"): row
        for row in _objects(
            correlation_one.get("components"), "correlation_one components"
        )
    }
    total_stress = _finite_float(correlation_one.get("loss"), "correlation_one loss")
    modes = _object(decomposition_lenses.get("modes"), "decomposition modes")

    rows = []
    for strategy in strategies:
        strategy_id = _text(strategy.get("strategy_id"), "underwriting strategy_id")
        strategy_name = _text(
            strategy.get("strategy_name"), "underwriting strategy_name"
        )
        live_factor = _factor_row_for_strategy(
            decomposition_lenses, "LIVE_ONLY", strategy_name
        )
        hybrid_factor = _factor_row_for_strategy(
            decomposition_lenses, "LIVE_PLUS_BACKTEST", strategy_name
        )
        history_members = _version_matched_history_observations(strategy, hybrid_factor)
        matched_backtest_observations = history_members or 0
        long_history_status = (
            "AVAILABLE_VERSION_MATCHED_BACKTEST_PLUS_LIVE"
            if history_members is not None
            else "UNAVAILABLE_NO_VERSION_MATCHED_BACKTEST_PREHISTORY"
        )
        lens_evidence: list[dict[str, Any]] = []
        for mode in ("LIVE_ONLY", "LIVE_PLUS_BACKTEST"):
            if modes.get(mode) is None:
                continue
            for lens in _objects(modes[mode], f"decomposition mode {mode}"):
                series_row = next(
                    (
                        item
                        for item in _objects(lens.get("series"), "decomposition series")
                        if item.get("series_id") == strategy_name
                    ),
                    None,
                )
                if series_row is None:
                    continue
                coverage = (
                    _object(series_row.get("coverage"), "underwriting coverage")
                    if series_row.get("coverage") is not None
                    else {}
                )
                quality = _object(
                    series_row.get("model_quality", {}),
                    "underwriting model quality",
                )
                lens_evidence.append(
                    {
                        "evidence_mode": mode,
                        "lens_id": _text(lens.get("lens_id"), "underwriting lens_id"),
                        "status": _text(
                            series_row.get("status"), "underwriting lens status"
                        ),
                        "aligned_observations": coverage.get("aligned_observations"),
                        "holdout_predicted_r_squared": quality.get(
                            "holdout_predicted_r_squared"
                        ),
                    }
                )

        gap = _model_account_gap_underwriting(strategy, gap_policy)
        regimes = _regime_underwriting(
            strategy,
            weekly_rows,
            regime_policy,
            matched_backtest_observations=matched_backtest_observations,
        )
        design_status = str(strategy.get("design_status") or "UNAVAILABLE")
        design_authority_status = (
            "AUTHORITATIVE_DESIGN_AVAILABLE"
            if design_status == "EVIDENCED"
            else "INFERRED_ECONOMIC_ARCHETYPE_ONLY"
        )
        design_conformance_status = (
            "AUTHORITATIVE_DESIGN_AND_EMPIRICAL_PREHISTORY_COMPARABLE"
            if design_authority_status == "AUTHORITATIVE_DESIGN_AVAILABLE"
            and regimes["status"] == "AVAILABLE_VERSION_MATCHED_PREHISTORY_COMPARISON"
            else "DIAGNOSTIC_ONLY_NOT_AUTHORITATIVE_DESIGN_CONFORMANCE"
        )

        cluster = next(
            (item for item in clusters if strategy_name in item.get("members", [])),
            None,
        )
        component = components.get(strategy_name)
        loss_contribution = (
            _finite_float(
                component.get("loss_contribution"),
                f"loss contribution {strategy_name}",
            )
            if component is not None
            else None
        )
        portfolio_fit_status = (
            "CLUSTER_BUDGET_BREACH"
            if cluster and str(cluster.get("budget_status", "")).startswith("BREACHES")
            else "WITHIN_CURRENT_CLUSTER_STRESS_BOUNDARY"
        )
        portfolio_fit = {
            "cluster_id": cluster.get("cluster_id") if cluster else None,
            "cluster_label": cluster.get("label") if cluster else None,
            "cluster_budget_status": cluster.get("budget_status") if cluster else None,
            "cluster_allocation_weight": cluster.get("allocation_weight")
            if cluster
            else None,
            "cluster_loss_contribution": cluster.get("loss_contribution")
            if cluster
            else None,
            "cluster_observed_pair_correlation_minimum": (
                cluster.get("observed_pair_correlation_minimum") if cluster else None
            ),
            "cluster_observed_pair_correlation_maximum": (
                cluster.get("observed_pair_correlation_maximum") if cluster else None
            ),
            "strategy_systematic_weight": strategy.get("systematic_weight"),
            # The two weights come from two sets: the strategy's current cutoff weight
            # and the policy weights behind the cluster budget.
            "strategy_weight_basis": strategy.get("weight_basis"),
            "cluster_weight_basis": WEIGHT_BASIS_POLICY if cluster else None,
            "strategy_correlation_one_loss_contribution": (
                _number_text(loss_contribution)
                if loss_contribution is not None
                else None
            ),
            "strategy_share_of_correlation_one_loss": (
                _number_text(loss_contribution / total_stress)
                if loss_contribution is not None and total_stress
                else None
            ),
            "leave_one_out_correlation_one_loss": (
                _number_text(total_stress - loss_contribution)
                if loss_contribution is not None
                else None
            ),
            "leave_one_out_stress_reduction": (
                _number_text(loss_contribution)
                if loss_contribution is not None
                else None
            ),
            "portfolio_fit_status": portfolio_fit_status,
        }

        recommendation_reasons: list[str] = []
        total_gap = _optional_float(
            gap.get("total_gap"), f"underwriting total gap {strategy_name}"
        )
        if strategy.get("hwm_scope") == "EXCLUDED_DE_MINIMIS":
            advisory_action = "DE_MINIMIS_VISIBLE_NO_RESEARCH_PRIORITY"
            recommendation_reasons.append("EXPLICIT_DE_MINIMIS_HWM_EXCLUSION")
        elif total_gap is not None and total_gap <= -0.05:
            advisory_action = "REVIEW_IMPLEMENTATION_GAP_BEFORE_CAPITAL_DECISION"
            recommendation_reasons.extend(
                ["MODEL_ACCOUNT_GAP_AT_OR_BELOW_MINUS_FIVE_PERCENT", gap["signature"]]
            )
        elif portfolio_fit_status == "CLUSTER_BUDGET_BREACH":
            advisory_action = "REVIEW_SHARED_CLUSTER_BUDGET_NOT_PROVIDER_LABEL"
            recommendation_reasons.append("SHARED_ECONOMIC_DRIVER_CLUSTER_BREACH")
        elif long_history_status != "AVAILABLE_VERSION_MATCHED_BACKTEST_PLUS_LIVE":
            advisory_action = (
                "RETAIN_MONITORING_INSUFFICIENT_LONG_HISTORY_FOR_DROP_DECISION"
            )
            recommendation_reasons.append("NO_VERSION_MATCHED_BACKTEST_PREHISTORY")
        else:
            advisory_action = "RETAIN_MONITORING_NO_DROP_AUTHORITY"
            recommendation_reasons.append("NO_AUTOMATED_DROP_RULE_TRIGGERED")
        if design_authority_status != "AUTHORITATIVE_DESIGN_AVAILABLE":
            recommendation_reasons.append("AUTHORITATIVE_DESIGN_INTENT_UNAVAILABLE")

        evidence = strategy.get("provider_evidence")
        provider_evidence = (
            _object(evidence, f"provider evidence {strategy_name}")
            if evidence is not None
            else {}
        )
        implementation_value = strategy.get("implementation_reconciliation")
        stale_reconciliation = (
            _validate_stale_implementation_object(
                implementation_value, kind="RECONCILIATION", strategy_id=strategy_id
            )
            if _is_stale_implementation(implementation_value)
            else None
        )
        implementation_detail = (
            {"stale_reconciliation": stale_reconciliation}
            if stale_reconciliation is not None
            else _object(
                implementation_value,
                f"implementation reconciliation {strategy_name}",
            )
            if implementation_value is not None
            else None
        )
        return_attribution_value = strategy.get("implementation_return_attribution")
        stale_attribution = (
            _validate_stale_implementation_object(
                return_attribution_value,
                kind="RETURN_ATTRIBUTION",
                strategy_id=strategy_id,
            )
            if _is_stale_implementation(return_attribution_value)
            else None
        )
        return_attribution_detail = (
            _object(
                return_attribution_value,
                f"implementation return attribution {strategy_name}",
            )
            if return_attribution_value is not None and stale_attribution is None
            else None
        )
        if stale_attribution is not None:
            implementation_detail = {
                **(implementation_detail or {}),
                "stale_return_attribution": stale_attribution,
            }
        elif return_attribution_detail is not None:
            implementation_detail = {
                **(implementation_detail or {}),
                "return_attribution": return_attribution_detail,
            }
        if stale_reconciliation is not None or stale_attribution is not None:
            implementation_comparison_status: object = STALE_IMPLEMENTATION_STATUS
        elif implementation_detail is None:
            implementation_comparison_status = gap["comparison_status"]
        elif implementation_detail.get("return_reconciliation") is not None:
            implementation_comparison_status = _object(
                implementation_detail["return_reconciliation"],
                f"implementation metrics {strategy_name}",
            ).get("status")
        else:
            # A comparison that is missing is not ready; it is never read as a
            # legacy comparison or borrowed from the other contract kind.
            implementation_comparison_status = NO_RETURN_RECONCILIATION_STATUS
        contract_context = _implementation_contract_context(
            strategy_id,
            {
                "reconciliation": implementation_value is not None,
                "return_attribution": return_attribution_value is not None,
            },
            implementation_contracts or {},
        )

        def factor_value(
            row: Mapping[str, Any] | None, container: str, field: str
        ) -> object:
            if row is None or row.get(container) is None:
                return None
            return _object(
                row.get(container), f"underwriting {container} {strategy_name}"
            ).get(field)

        rows.append(
            {
                "strategy_id": strategy_id,
                "strategy_name": strategy_name,
                "evidence_layers": {
                    "design": {
                        "status": design_status,
                        "authority_status": design_authority_status,
                        "version_ref": strategy.get("design_version_ref"),
                        "economic_family": strategy.get("economic_family"),
                        "implementation_lineage": strategy.get(
                            "implementation_lineage"
                        ),
                    },
                    "backtest": {
                        "status": strategy.get("backtest_status"),
                        "version_ref": strategy.get("history_version_ref")
                        or strategy.get("backtest_version_ref"),
                        "compatibility_status": strategy.get(
                            "history_compatibility_status"
                        ),
                        "compatibility_basis": strategy.get(
                            "history_compatibility_reason"
                        ),
                        "matched_observations": matched_backtest_observations,
                    },
                    "provider_or_model_live": {
                        "status": (
                            "UNAVAILABLE_POLICY_NOT_ADMITTED"
                            if gap["comparison_status"]
                            == "UNAVAILABLE_POLICY_NOT_ADMITTED"
                            else "AVAILABLE"
                            if provider_evidence.get("provider") is not None
                            or gap["total_gap"] is not None
                            else "UNAVAILABLE"
                        ),
                        "comparison_status": gap["comparison_status"],
                        "observed_start": gap["observed_start"],
                        "observed_end": gap["observed_end"],
                    },
                    "account_live": {
                        "status": strategy.get("account_live_status"),
                        "observed_start": strategy.get("account_live_observed_start"),
                        "observed_end": strategy.get("account_live_observed_end"),
                        "observations": strategy.get(
                            "account_live_return_observations"
                        ),
                        "cumulative_return": strategy.get(
                            "account_live_cumulative_return"
                        ),
                        "max_drawdown": strategy.get("account_live_max_drawdown"),
                    },
                    "implementation_reconciliation": {
                        "status": strategy.get("implementation_reconciliation_status")
                        or gap["status"],
                        "comparison_status": implementation_comparison_status,
                        "contracts": contract_context,
                        "detail": implementation_detail,
                    },
                },
                "design_fit": {
                    "status": design_authority_status,
                    "economic_family": strategy.get("economic_family"),
                    "portfolio_role": cluster.get("label") if cluster else None,
                    "role_authority": (
                        "GOVERNED_ECONOMIC_DRIVER_CLUSTER"
                        if cluster is not None
                        else "UNAVAILABLE"
                    ),
                    "conformance_status": design_conformance_status,
                },
                "long_history": {
                    "status": long_history_status,
                    "matched_backtest_observations": matched_backtest_observations,
                    "membership_rule": LONG_HISTORY_MEMBERSHIP_RULE,
                    "lens_statistics_source": {
                        "lens_id": "ECONOMIC_FACTOR",
                        "live_source_key": (
                            live_factor.get("source_key")
                            if live_factor is not None
                            else None
                        ),
                        "hybrid_source_key": (
                            hybrid_factor.get("source_key")
                            if hybrid_factor is not None
                            else None
                        ),
                    },
                    "live_aligned_observations": factor_value(
                        live_factor, "coverage", "aligned_observations"
                    ),
                    "hybrid_aligned_observations": factor_value(
                        hybrid_factor, "coverage", "aligned_observations"
                    ),
                    "live_net_alpha": factor_value(
                        live_factor, "alpha", "net_annualized"
                    ),
                    "hybrid_net_alpha": factor_value(
                        hybrid_factor, "alpha", "net_annualized"
                    ),
                    "live_net_alpha_window": _alpha_window(live_factor),
                    "hybrid_net_alpha_window": _alpha_window(hybrid_factor),
                    "live_holdout_predicted_r_squared": factor_value(
                        live_factor,
                        "model_quality",
                        "holdout_predicted_r_squared",
                    ),
                    "hybrid_holdout_predicted_r_squared": factor_value(
                        hybrid_factor,
                        "model_quality",
                        "holdout_predicted_r_squared",
                    ),
                    "lens_evidence": lens_evidence,
                },
                "model_account_gap": gap,
                "regime_conformance": regimes,
                "portfolio_fit": portfolio_fit,
                "advisory_disposition": {
                    "action": advisory_action,
                    "reasons": recommendation_reasons,
                    "drop_decision_status": "NO_AUTOMATIC_DROP_AUTHORITY",
                    "allocation_effect": "NONE",
                    "execution_effect": "NONE",
                },
            }
        )
    if len(rows) != len(strategies) or {row["strategy_id"] for row in rows} != {
        str(row.get("strategy_id")) for row in strategies
    }:
        raise ControlPanelValidationError("underwriting strategy boundary differs")
    return (
        {
            "schema_version": 1,
            "policy_id": policy["policy_id"],
            "policy_sha256": policy_hash,
            "as_of_date": portfolio_as_of,
            "authority_boundary": _object(
                policy.get("decision_boundary"), "underwriting decision boundary"
            ),
            "methodology": {
                "evidence_layer_rule": "SEPARATE_NEVER_SILENTLY_CONCATENATED",
                "long_history_rule": (
                    "VERSION_BOUND_HISTORY_FOR_INFERENCE_SEPARATE_FROM_"
                    "IMPLEMENTATION_RECONCILIATION"
                ),
                "gap_rule": gap_policy.get("causal_component_rule"),
                "regime_rule": regime_policy.get("threshold_estimation"),
                "portfolio_fit_rule": _object(
                    policy.get("portfolio_fit"), "portfolio fit policy"
                ).get("cluster_rule"),
            },
            "counts": {
                "strategies": len(rows),
                "authoritative_design": sum(
                    row["design_fit"]["status"] == "AUTHORITATIVE_DESIGN_AVAILABLE"
                    for row in rows
                ),
                "version_matched_long_history": sum(
                    row["long_history"]["status"]
                    == "AVAILABLE_VERSION_MATCHED_BACKTEST_PLUS_LIVE"
                    for row in rows
                ),
                "quantified_model_account_gap": sum(
                    row["model_account_gap"]["total_gap"] is not None for row in rows
                ),
                "empirical_regime_comparison": sum(
                    row["regime_conformance"]["status"]
                    == "AVAILABLE_VERSION_MATCHED_PREHISTORY_COMPARISON"
                    for row in rows
                ),
            },
            "rows": rows,
        },
        policy_hash,
    )


def _reconciliation_passed(rows: Sequence[Mapping[str, Any]]) -> int:
    """How many reconciliation checks passed: every status that starts with ``PASS``.

    ``PASS_WITH_EXPLICIT_RECONCILIATION`` is a pass with its reconciliation stated; the
    shell paints it as accepted and the coverage verification accepts it, so the counter
    counts it too (B11).
    """

    return sum(str(row.get("status", "")).startswith("PASS") for row in rows)


def _default_lens(lenses: Mapping[str, Any]) -> dict[str, Any]:
    """The default evidence mode's default lens, as the panel opens on it."""

    mode = _text(lenses.get("default_evidence_mode"), "default evidence mode")
    lens_id = _text(lenses.get("default_lens"), "default lens")
    modes = _object(lenses.get("modes"), "decomposition lens modes")
    lens = next(
        (
            row
            for row in _objects(modes.get(mode), f"decomposition lenses {mode}")
            if row.get("lens_id") == lens_id
        ),
        None,
    )
    if lens is None:
        raise ControlPanelValidationError(
            f"default decomposition lens is absent from its mode: {lens_id}"
        )
    return lens


def _rendered_default_lens_rows(
    lenses: Mapping[str, Any],
) -> tuple[str, str | None, list[dict[str, Any]]]:
    """The lens, source and series rows the panel shows by default.

    The default lens narrowed to its default source the way the renderer filters it:
    a row with no source key is shown under every source.
    """

    lens = _default_lens(lenses)
    lens_id = _text(lens.get("lens_id"), "default lens id")
    source_key = lens.get("default_source_key")
    if source_key is not None:
        source_key = _text(source_key, "default source key")
    rows = [
        row
        for row in _objects(lens.get("series"), f"{lens_id} series")
        if source_key is None
        or not row.get("source_key")
        or row.get("source_key") == source_key
    ]
    return lens_id, source_key, rows


def _lens_is_source_qualified(rows: Sequence[Mapping[str, Any]]) -> bool:
    """Whether every source of the lens lists the same strategies (current plan)."""

    by_source: dict[str, set[str]] = {}
    for row in rows:
        source_key = row.get("source_key")
        if not source_key:
            return False
        by_source.setdefault(str(source_key), set()).add(str(row.get("series_id")))
    return (
        len(by_source) > 0 and len({frozenset(ids) for ids in by_source.values()}) == 1
    )


HWM_CANDIDATE_LABEL: Final = "Governed PORT-HWM-4 candidate, pending owner acceptance"


def _hwm_candidate(
    proof: Mapping[str, Any] | None, hwm_summary: Mapping[str, Any]
) -> dict[str, Any] | None:
    """The governed number as a labelled candidate beside the numeric status.

    The numeric status stays what the report says: the E2 register (two Robuxio
    two-day spans can hide a peak) keeps it unaccepted, and the governed PORT-HWM-4
    receipt is pending owner acceptance.  The panel shows that number under its own
    label instead of hiding it, so no "no number is calculated" text sits beside it.
    """

    if proof is None or proof.get("status") != "AVAILABLE":
        return None
    if proof.get("financial_acceptance") != "PENDING_OWNER_ACCEPTANCE":
        raise ControlPanelValidationError(
            "governed HWM proof carries an unexpected financial acceptance state"
        )
    unaccepted = hwm_summary.get("hwm_numerical_status", "UNAVAILABLE") == "UNAVAILABLE"
    return {
        "status": "CANDIDATE_PENDING_OWNER_ACCEPTANCE",
        "policy_id": proof["policy_id"],
        "current_drawdown": proof["current_drawdown"],
        "maximum_drawdown": proof["maximum_drawdown"],
        "current_index": proof["current_index"],
        "current_hwm": proof["current_hwm"],
        "complete_through": proof["complete_through"],
        "evaluated_on": proof["evaluated_on"],
        "financial_acceptance": proof["financial_acceptance"],
        "not_accepted_reason": (
            hwm_summary.get("hwm_numerical_reason") if unaccepted else None
        ),
        "not_accepted_register": "E2",
        "receipt_sha256": proof["receipt_sha256"],
        "label": HWM_CANDIDATE_LABEL,
    }


POSITION_CURRENT_BASIS: Final = "LATEST_ACCEPTED_ACCOUNT_SNAPSHOT_AT_CUTOFF"
_POSITION_LATEST: Final = "LATEST_ACCEPTED_ACCOUNT_SNAPSHOT"
_POSITION_HISTORICAL: Final = "HISTORICAL_ACCEPTED_ACCOUNT_SNAPSHOT"

# E1: a position's listing identity is the report's ``holdings_identity`` sidecar
# row for the same (account_id, as_of_date, security_id).  The panel states what
# that row proves and nothing more: a symbol the sidecar proves is shown, a symbol
# it cannot prove stays NOT_READY with the sidecar's own reason code, and a report
# without a sidecar says so.  A position and a sidecar row that disagree are a
# build error, never a reconciled value.
SYMBOL_STATUS_NOT_READY_NO_SIDECAR: Final = "HOLDINGS_IDENTITY_SIDECAR_ABSENT"
_POSITION_ACCEPTED_STATUS_PREFIX: Final = "AVAILABLE"
_IDENTITY_JOIN_FIELDS: Final = ("quantity", "strategy_id")


def _holdings_identity_index(
    report: Mapping[str, Any],
) -> dict[tuple[str, str, str], Mapping[str, Any]] | None:
    """The sidecar rows keyed by (account_id, as_of_date, security_id), or None."""

    section = report.get("holdings_identity")
    if section is None:
        return None
    if not isinstance(section, Mapping):
        raise ControlPanelValidationError("holdings_identity is not an object")
    index: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for item in _objects(section.get("rows"), "holdings_identity.rows"):
        key = (
            _text(item.get("account_id"), "holdings_identity account_id"),
            _text(item.get("as_of_date"), "holdings_identity as_of_date"),
            _text(item.get("security_id"), "holdings_identity security_id"),
        )
        if key in index:
            raise ControlPanelValidationError(
                f"holdings_identity has two rows for {key}"
            )
        index[key] = item
    return index


def _identity_row_for(
    row: Mapping[str, Any],
    index: Mapping[tuple[str, str, str], Mapping[str, Any]],
) -> Mapping[str, Any]:
    """The sidecar row of one accepted position; refuse when it does not match."""

    key = (
        str(row.get("account_id")),
        str(row.get("as_of_date")),
        str(row.get("security_id")),
    )
    identity = index.get(key)
    if identity is None:
        raise ControlPanelValidationError(
            f"position {key} has no holdings_identity row"
        )
    for field in _IDENTITY_JOIN_FIELDS:
        if identity.get(field) != row.get(field):
            raise ControlPanelValidationError(
                f"position {key} differs from its holdings_identity row on {field}"
            )
    carried = row.get("symbol")
    if carried is not None and carried != identity.get("symbol"):
        raise ControlPanelValidationError(
            f"position {key} symbol differs from its holdings_identity row"
        )
    return identity


def _position_symbol_status_and_reason(
    row: Mapping[str, Any],
    identity_index: Mapping[tuple[str, str, str], Mapping[str, Any]] | None,
) -> tuple[str, str | None, str | None, str | None]:
    """State a position's symbol and asset category from its sidecar row.

    Returns ``(symbol_status, symbol_reason, symbol, asset_category)``.  AVAILABLE
    when the sidecar proves a symbol; NOT_READY with the sidecar's reason code when
    an accepted position has none; NOT_APPLICABLE when the row is not an accepted
    position (the no-position sentinel row).  Nothing is typed in: the symbol is
    the sidecar's, and an accepted position the sidecar does not describe refuses.
    """

    if not str(row.get("status", "")).startswith(_POSITION_ACCEPTED_STATUS_PREFIX):
        return "NOT_APPLICABLE", None, row.get("symbol"), None
    if identity_index is None:
        return (
            ("AVAILABLE", None, row.get("symbol"), None)
            if row.get("symbol") is not None
            else ("NOT_READY", SYMBOL_STATUS_NOT_READY_NO_SIDECAR, None, None)
        )
    identity = _identity_row_for(row, identity_index)
    category = identity.get("asset_category")
    if identity.get("symbol_status") == "AVAILABLE":
        return "AVAILABLE", None, _text(identity.get("symbol"), "symbol"), category
    reason = identity.get("symbol_unavailable_reason")
    return "NOT_READY", reason if isinstance(reason, str) else None, None, category


def _alpha_window(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The aligned window and training count one annualised alpha stands on."""

    coverage = None if row is None else row.get("coverage")
    if not isinstance(coverage, Mapping):
        return None
    start, end = coverage.get("observed_start"), coverage.get("observed_end")
    days = (
        (date.fromisoformat(end) - date.fromisoformat(start)).days
        if isinstance(start, str) and isinstance(end, str)
        else None
    )
    return {
        "start": start,
        "end": end,
        "days": days,
        "aligned_observations": coverage.get("aligned_observations"),
        "train_observations": coverage.get("train_observations"),
        "annualisation_basis": (
            None
            if days is None
            else "ANNUALISED_FROM_DAYS"
            if days < 365
            else "ANNUALISED"
        ),
    }


def _panel_schema_version_for(report_schema_version: object) -> int:
    """The panel version a report version selects: custody coverage reports are 25."""

    if report_schema_version in {3, 4, 5}:
        return CUSTODY_COVERAGE_PANEL_SCHEMA_VERSION
    return PANEL_SCHEMA_VERSION


def _classify_positions(
    rows: Sequence[Mapping[str, Any]], cutoff_date: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Split the accepted position rows into current holdings and labelled history.

    A row is current only when the report marks it the latest accepted snapshot of
    its account and that snapshot is dated at the calculation cutoff.  A latest
    snapshot dated before the cutoff is stale, a historical snapshot is history,
    and a row dated after the cutoff is named and never counted as current.
    """

    cutoff = _panel_date(cutoff_date, "position cutoff date")
    classified: list[dict[str, Any]] = []
    counts = Counter[str]()
    history_dates: set[str] = set()
    for row in rows:
        if not str(row.get("status", "")).startswith("AVAILABLE"):
            position_class = "UNAVAILABLE"
        else:
            snapshot_status = row.get("snapshot_status")
            if snapshot_status not in {_POSITION_LATEST, _POSITION_HISTORICAL}:
                raise ControlPanelValidationError(
                    "position row lacks a recognised snapshot_status: "
                    f"{snapshot_status!r}"
                )
            as_of_date = _text(row.get("as_of_date"), "position as_of_date")
            as_of = _panel_date(as_of_date, "position as_of_date")
            if as_of > cutoff:
                position_class = "AFTER_CUTOFF"
            elif snapshot_status == _POSITION_HISTORICAL:
                position_class = "HISTORICAL"
                history_dates.add(as_of_date)
            elif as_of == cutoff:
                position_class = "CURRENT_AT_CUTOFF"
            else:
                position_class = "LATEST_BEFORE_CUTOFF"
                history_dates.add(as_of_date)
        counts[position_class] += 1
        classified.append({**row, "position_class": position_class})
    summary = {
        "cutoff_date": cutoff.isoformat(),
        "current_basis": POSITION_CURRENT_BASIS,
        "current_at_cutoff_rows": counts["CURRENT_AT_CUTOFF"],
        "latest_before_cutoff_rows": counts["LATEST_BEFORE_CUTOFF"],
        "historical_rows": counts["HISTORICAL"],
        "after_cutoff_rows": counts["AFTER_CUTOFF"],
        "unavailable_rows": counts["UNAVAILABLE"],
        "accepted_rows_total": sum(
            counts[name]
            for name in (
                "CURRENT_AT_CUTOFF",
                "LATEST_BEFORE_CUTOFF",
                "HISTORICAL",
                "AFTER_CUTOFF",
            )
        ),
        "history_snapshot_dates": len(history_dates),
        "history_first_as_of": min(history_dates) if history_dates else None,
        "history_last_as_of": max(history_dates) if history_dates else None,
    }
    return classified, summary


def _position_cutoff_date(report: Mapping[str, Any]) -> str:
    cutoff = report.get("calculation_cutoff_date")
    return _text(
        cutoff if cutoff is not None else report.get("as_of_date"),
        "report position cutoff date",
    )


def _panel_data(
    report: Mapping[str, Any],
    report_identity: Mapping[str, str],
    decomposition: Mapping[str, Any],
    decomposition_identity: Mapping[str, str],
    correlation: Mapping[str, Any],
    correlation_identity: Mapping[str, str],
    legacy: Mapping[str, Any],
    source: Mapping[str, Any],
    template_root: Path,
    template_hash_snapshots: Mapping[str, str] | None = None,
    strategy_evidence_path: Path | None = None,
    strategy_evidence_snapshot: bytes | None = None,
    decomposition_lenses: Mapping[str, Any] | None = None,
    weekly_returns: Mapping[str, Any] | None = None,
    freshness_health: list[dict[str, Any]] | None = None,
    replication_payload: Mapping[str, Any] | None = None,
    candidate_driver_map: Mapping[str, Any] | None = None,
    candidate_driver_map_identity: Mapping[str, str] | None = None,
    baseline_catalog: Mapping[str, Any] | None = None,
    hwm_portable_receipt: Mapping[str, Any] | None = None,
    hwm_portable_generation_bytes: bytes | None = None,
    hwm_portable_calendar_bytes: bytes | None = None,
    hwm_governed_result: Mapping[str, Any] | None = None,
    hwm_governed_generation_bytes: bytes | None = None,
    hwm_governed_calendar_bytes: bytes | None = None,
    hwm_governed_receipt_bytes: bytes | None = None,
    account_manifest: Mapping[str, Any] | None = None,
    account_manifest_identity: Mapping[str, Any] | None = None,
    financial_contract_snapshot: bytes | None = None,
    financial_input_contract_snapshot: bytes | None = None,
    hwm_policy_snapshot: bytes | None = None,
    portable_hwm_policy_snapshot: bytes | None = None,
    portable_hwm_performance_policy_snapshot: bytes | None = None,
    economic_driver_snapshot: bytes | None = None,
    underwriting_policy_snapshot: bytes | None = None,
    correlation_strategy_names: Mapping[str, str] | None = None,
    epoch_hpr_receipts: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    if freshness_health is None:
        raise ControlPanelValidationError("freshness health is mandatory")
    if baseline_catalog is None:
        raise ControlPanelValidationError(
            "baseline catalog is mandatory for governed HWM reconciliation"
        )
    summary = _summary(report)
    report_strategies = _objects(report.get("strategy_summary"), "strategy_summary")
    strategy_product_controls = _financial_mvp_product_controls(
        financial_contract_snapshot
    )
    current_product_catalog = _validate_current_product_baseline_projection(
        report, baseline_catalog, strategy_product_controls
    )
    _validate_current_product_boundary(report, strategy_product_controls)
    strategies = _current_product_rows(report_strategies, strategy_product_controls)
    if CURRENT_PRODUCT_STRATEGY_IDS <= set(strategy_product_controls):
        strategy_ids = [
            _text(row.get("strategy_id"), "strategy.strategy_id") for row in strategies
        ]
        if len(strategy_ids) != len(set(strategy_ids)) or set(strategy_ids) != set(
            CURRENT_REQUIRED_STRATEGY_IDS
        ):
            raise ControlPanelValidationError("current strategy identities differ")
    accounts = _current_product_account_summary_rows(
        _objects(report.get("account_summary"), "account_summary"),
        baseline_catalog,
        strategy_product_controls,
    )
    allocations = _current_product_rows(
        _objects(report.get("allocation_history"), "allocation_history"),
        strategy_product_controls,
    )
    performance = _current_product_rows(
        _objects(report.get("twr_mwr"), "twr_mwr"), strategy_product_controls
    )
    positions_value = report.get("positions", [])
    report_positions = (
        [] if positions_value is None else _objects(positions_value, "positions")
    )
    positions = _current_product_rows(report_positions, strategy_product_controls)
    performance_lineage = _performance_lineage(account_manifest, baseline_catalog)
    performance_lineage_available = (
        performance_lineage["status"] != "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH"
    )
    coverage = [*_fact_rows(report), *(freshness_health or [])]
    freshness_evaluated_dates = sorted(
        {
            _text(row.get("evaluated_on"), "freshness.evaluated_on")
            for row in freshness_health
        }
    )
    if len(freshness_evaluated_dates) != 1:
        raise ControlPanelValidationError(
            "freshness health must share one operational evaluation date"
        )
    freshness_evaluated_on = freshness_evaluated_dates[0]
    reconciliations = _objects(
        report.get("reconciliation_checks"), "reconciliation_checks"
    )
    fact_coverage_matrix = _objects(
        report.get("fact_coverage_matrix"), "fact_coverage_matrix"
    )
    if not fact_coverage_matrix:
        raise ControlPanelValidationError("fact coverage matrix is unavailable")
    report_capital_expectations = _report_panel_expectations(report)
    if report_capital_expectations.get("custody_current_capital") != (
        baseline_catalog.get("current_capital")
        if baseline_catalog.get("current_capital", {}).get("custody_authority")
        else None
    ):
        raise ControlPanelValidationError(
            "custody report differs from selected baseline"
        )
    # The capital block the panel publishes is the governed baseline's current
    # capital as the report carried it; the report's rows are reconciled with the
    # baseline here and with the panel's own strategy rows below.
    capital_parts = _capital_summary(summary)
    _reconcile_capital_with_baseline(capital_parts, baseline_catalog)
    account_ids = [
        _text(row.get("account_id"), "account_summary.account_id") for row in accounts
    ]
    if len(set(account_ids)) != len(account_ids):
        raise ControlPanelValidationError("current account identities are not unique")
    if account_manifest is not None:
        _validate_current_product_account_manifest(
            account_manifest, strategy_product_controls, set(account_ids)
        )
    for row in accounts:
        provider = row.get("provider_id", row.get("provider"))
        _text(provider, "account_summary.provider")
        if any(
            field in row
            for field in (
                "account_number",
                "external_account_id",
                "masked_account_number",
            )
        ):
            raise ControlPanelValidationError(
                "external account identifiers cannot enter panel data"
            )
    derived_product_account_ids = (
        {
            _text(row.get("account_id"), "account_manifest.account_id")
            for row in _split_manifest_epoch_rows(
                _objects(account_manifest.get("rows"), "account_manifest.rows"),
                _current_epoch_crosswalk_included_tuples(
                    account_manifest_identity["current_epoch_crosswalk"]
                ),
                strategy_product_controls,
            )[0]
        }
        - set(account_ids)
        if account_manifest is not None
        and account_manifest_identity is not None
        and account_manifest_identity.get("current_epoch_crosswalk") is not None
        else set()
    )
    performance = _current_account_rows(
        performance, set(account_ids) | derived_product_account_ids
    )
    positions = _current_account_rows(positions, set(account_ids))
    if account_manifest is not None:
        _validate_current_product_epoch_surface(
            account_manifest,
            performance,
            strategy_product_controls,
            as_of_date=_text(report.get("as_of_date"), "report.as_of_date"),
            current_epoch_crosswalk=(
                account_manifest_identity.get("current_epoch_crosswalk")
                if account_manifest_identity is not None
                else None
            ),
        )
    if not performance_lineage_available:
        # Validate the original report facts and dates before rendering their
        # rejected-lineage representation; otherwise a post-cutoff value could
        # be erased and silently admitted.
        performance = _suppress_unmatched_report_performance(performance)
    _validate_mwr_perimeter(performance)
    referenced_account_ids = _referenced_current_account_ids((*performance, *positions))
    missing_account_ids = (
        referenced_account_ids - set(account_ids) - derived_product_account_ids
    )
    if missing_account_ids:
        raise ControlPanelValidationError(
            "panel facts reference accounts outside account_summary: "
            + ", ".join(sorted(missing_account_ids))
        )

    report_strategy_names = {
        _text(row.get("strategy_id"), "strategy.strategy_id"): _text(
            row.get("strategy_name"), "strategy.strategy_name"
        )
        for row in report_strategies
    }
    strategy_names = {
        _text(row.get("strategy_id"), "strategy.strategy_id"): _text(
            row.get("strategy_name"), "strategy.strategy_name"
        )
        for row in strategies
    }
    dependence_strategy_names = _dependence_strategy_names(
        strategy_names, financial_contract_snapshot
    )
    dependence_strategies = [
        row
        for row in strategies
        if _text(row.get("strategy_name"), "strategy.strategy_name")
        in dependence_strategy_names
    ]
    hybrid_mode_gate = _hybrid_mode_gate(dependence_strategies)
    # Partial coverage publishes. `_hybrid_mode_gate` returns
    # ELIGIBLE_PARTIAL_VERSION_COMPATIBILITY when some strategies have an
    # accepted version-bound history and others do not, and that is the status
    # the published 2026-08-16 panel (control-panel generation 41f41362) shipped
    # under: LIVE_PLUS_BACKTEST enabled for five strategy history paths, four
    # blocked and live-only inside the hybrid mode. Requiring every dependence
    # strategy to be eligible discarded that status and silently downgraded the
    # whole release to LIVE_ONLY, which the contract's REQUIRED evidence_modes
    # then refuse. A strategy without accepted prehistory is still represented;
    # it is represented by its live-only segments, which is what the gate's
    # blocked list records.
    globally_hybrid_eligible = hybrid_mode_gate["status"] in {
        "ELIGIBLE_PROVEN_COMPATIBLE",
        "ELIGIBLE_PARTIAL_VERSION_COMPATIBILITY",
    }
    evidence_by_strategy, evidence_identity = _strategy_evidence(
        report,
        report_identity,
        strategy_evidence_path,
        set(report_strategy_names),
        strategy_evidence_snapshot,
    )
    evidence_by_strategy = {
        strategy_id: evidence
        for strategy_id, evidence in evidence_by_strategy.items()
        if strategy_id in strategy_names
    }
    current_strategy_names = set(strategy_names.values())
    accepted_replication = (
        dict(replication_payload)
        if replication_payload is not None
        else {
            "analytics_hash": None,
            "as_of_date": _text(report.get("as_of_date"), "report.as_of_date"),
            "authority_boundary": {
                "allocation_authority": "NONE",
                "execution_authority": "NONE",
                "hwm_authority": "NONE",
                "readiness_gate_effect": "NONE",
                "use": "RESEARCH_DIAGNOSTIC_ONLY",
            },
            "available_count": 0,
            "clean_room_boundary": (
                "Dynamic research replication was not configured for this build."
            ),
            "diagnostic_count": 0,
            "generation_id": None,
            "method_label": "MPI_INFORMED_NON_EQUIVALENT_RESEARCH_REPLICATION",
            "series": [
                {
                    "availability_status": "UNAVAILABLE_NOT_CONFIGURED",
                    "blocking_reasons": ["REPLICATION_ROOT_NOT_SUPPLIED"],
                    "model_id": None,
                    "series_id": name,
                }
                for name in sorted(current_strategy_names)
            ],
            "source_sha256": None,
            "unavailable_count": len(current_strategy_names),
        }
    )
    factor_analysis, dynamic_factor_analysis = _current_primary_factor_analyses(
        decomposition,
        decomposition_identity,
        strategy_names,
        financial_contract_snapshot,
    )
    if decomposition_lenses is None:
        accepted_decomposition_lenses = {
            "default_evidence_mode": "LIVE_ONLY",
            "default_lens": "DYNAMIC_STYLE",
            "evidence_modes": ["LIVE_ONLY"],
            "lenses": [dynamic_factor_analysis],
            "modes": {"LIVE_ONLY": [dynamic_factor_analysis]},
            "plan_bundle_id": None,
            "plan_manifest_sha256": None,
        }
    else:
        accepted_decomposition_lenses = dict(decomposition_lenses)
        lens_rows = _objects(
            accepted_decomposition_lenses.get("lenses"), "decomposition lenses"
        )
        lens_ids = {
            _text(row.get("lens_id"), "decomposition lens_id") for row in lens_rows
        }
        if lens_ids != {
            "ECONOMIC_FACTOR",
            "ETF_STYLE",
            "RETURN_INDUSTRY",
            "RETURN_SECTOR",
        }:
            raise ControlPanelValidationError("decomposition lens set differs")
        accepted_decomposition_lenses["lenses"] = [
            *list(lens_rows),
            dynamic_factor_analysis,
        ]
        raw_modes = accepted_decomposition_lenses.get("modes")
        if raw_modes is not None:
            modes = _object(raw_modes, "decomposition lens modes")
            live_mode = _objects(modes.get("LIVE_ONLY"), "live decomposition lenses")
            if {
                _text(row.get("lens_id"), "live decomposition lens_id")
                for row in live_mode
            } != lens_ids:
                raise ControlPanelValidationError(
                    "live decomposition mode lens set differs"
                )
            accepted_modes = dict(modes)
            accepted_modes["LIVE_ONLY"] = [*live_mode, dynamic_factor_analysis]
            if not globally_hybrid_eligible:
                accepted_modes = {"LIVE_ONLY": accepted_modes["LIVE_ONLY"]}
                accepted_decomposition_lenses["evidence_modes"] = ["LIVE_ONLY"]
            hybrid_mode = accepted_modes.get("LIVE_PLUS_BACKTEST")
            if hybrid_mode is not None:
                hybrid_lenses = _objects(hybrid_mode, "hybrid decomposition lenses")
                _validate_hybrid_decomposition_lenses(hybrid_lenses, lens_ids)
                eligible_versions = {
                    _text(item.get("strategy_name"), "eligible strategy_name"): _text(
                        item.get("version_ref"), "eligible version_ref"
                    )
                    for item in _objects(
                        hybrid_mode_gate.get("eligible_strategies"),
                        "eligible strategies",
                    )
                }
                blocked_names = {
                    _text(item.get("strategy_name"), "blocked strategy_name")
                    for item in _objects(
                        hybrid_mode_gate.get("blocked_strategies"),
                        "blocked strategies",
                    )
                }
                for lens in hybrid_lenses:
                    for series in _objects(
                        lens.get("series"), "hybrid decomposition series"
                    ):
                        strategy_name = _text(
                            series.get("series_id"), "hybrid decomposition series_id"
                        )
                        if strategy_name in eligible_versions:
                            # A lens the strategy is method-scoped out of still
                            # emits a row for it, with no model and no
                            # composition. There is no hybrid version to check
                            # because the lens decomposed nothing, and a row
                            # without a model cannot have consumed backtest
                            # history.
                            if (
                                series.get("model_id") is None
                                and _text(
                                    series.get("status"), "hybrid decomposition status"
                                )
                                == "UNAVAILABLE"
                            ):
                                continue
                            composition = _object(
                                series.get("series_composition"),
                                f"hybrid decomposition composition {strategy_name}",
                            )
                            if (
                                composition.get("strategy_version")
                                != eligible_versions[strategy_name]
                            ):
                                raise ControlPanelValidationError(
                                    "eligible decomposition hybrid version differs "
                                    f"from registry: {strategy_name}"
                                )
                        elif strategy_name in blocked_names:
                            raw_composition = series.get("series_composition")
                            if raw_composition is None:
                                continue
                            composition = _object(
                                raw_composition,
                                f"hybrid decomposition composition {strategy_name}",
                            )
                            if (
                                _finite_float(
                                    composition.get("backtest_observations_used", 0),
                                    "hybrid decomposition backtest observations",
                                )
                                != 0
                            ):
                                raise ControlPanelValidationError(
                                    "unresolved strategy uses backtest history in hybrid "
                                    f"decomposition: {strategy_name}"
                                )
                        else:
                            raise ControlPanelValidationError(
                                "hybrid decomposition strategy is outside registry gate: "
                                f"{strategy_name}"
                            )
            accepted_decomposition_lenses["modes"] = accepted_modes
    accepted_weekly_returns = (
        dict(weekly_returns)
        if weekly_returns is not None
        else {
            "analytics_schema_version": 2,
            "as_of_date": _text(report.get("as_of_date"), "report.as_of_date"),
            "current_week": {
                "data_through_date": None,
                "default_visible": True,
                "is_partial": False,
                "week_end": None,
                "week_start": None,
            },
            "default_evidence_mode": "LIVE_ONLY",
            "display_groups": [],
            "generation_id": None,
            "observation_meta_fields": [
                "daily_observation_count",
                "actual_observed_start",
                "actual_observed_end",
                "coverage_status",
                "boundary_reasons",
            ],
            "series": [],
            "source_sha256": None,
            "status": "UNAVAILABLE_NOT_CONFIGURED",
            "strategy_evidence_modes": ["LIVE_ONLY", "LIVE_PLUS_BACKTEST"],
            "week_policy": {
                "absent_value": "UNAVAILABLE_NOT_ZERO",
                "aggregation": "PRODUCT_OF_ONE_PLUS_DAILY_RETURN_MINUS_ONE",
            },
            "weeks": [],
        }
    )
    accepted_weekly_returns = _govern_weekly_hybrid_modes(
        accepted_weekly_returns, hybrid_mode_gate
    )
    source_correlation = correlation
    source_raw_modes = source_correlation.get("dependence_modes")
    report_dependence_raw = report.get("dependence_analysis")
    report_dependence = (
        {"status": "UNAVAILABLE_NOT_CONFIGURED"}
        if report_dependence_raw is None
        else _object(report_dependence_raw, "report dependence_analysis")
    )
    if report_dependence.get("status") == "AVAILABLE":
        report_mode_payload = _object(source_raw_modes, "dependence_modes")
        report_dependence_identity = _object(
            report_dependence.get("identity"), "report dependence identity"
        )
        if report_dependence_identity != {
            "correlation_sha256": correlation_identity["sha256"],
            "generation_id": correlation_identity["generation_id"],
            "manifest_sha256": correlation_identity["manifest_sha256"],
        }:
            raise ControlPanelValidationError(
                "report dependence identity differs from correlation authority"
            )
        if (
            report_dependence.get("analytics_hash")
            != source_correlation.get("analytics_hash")
            or report_dependence.get("mode_payload_sha256")
            != hashlib.sha256(canonical_json_bytes(report_mode_payload)).hexdigest()
            or report_dependence.get("pair_count") != 21
            or report_dependence.get("pair_mode_count") != 42
            or set(report_dependence.get("series_ids", [])) != dependence_strategy_names
        ):
            raise ControlPanelValidationError(
                "report dependence projection differs from correlation authority"
            )
    correlation = _current_product_correlation_projection(
        source_correlation,
        correlation_strategy_names or report_strategy_names,
        include_hybrid=globally_hybrid_eligible,
    )
    correlation_projection_applied = (
        correlation.get("_current_product_projection_applied") is True
    )
    raw_modes = correlation.get("dependence_modes")
    if raw_modes is None:
        default_dependence_mode = "LIVE_ONLY"
        dependence_analysis = _dependence_analysis(
            correlation,
            correlation_identity,
            current_strategy_names,
            evidence_mode=default_dependence_mode,
            economic_contract_snapshot=economic_driver_snapshot,
        )
        dependence_modes = {default_dependence_mode: dependence_analysis}
    else:
        if correlation.get("analytics_schema_version") != 2:
            raise ControlPanelValidationError(
                "dependence modes require correlation schema version 2"
            )
        default_dependence_mode = _text(
            correlation.get("default_evidence_mode"), "default_evidence_mode"
        )
        if default_dependence_mode != "LIVE_ONLY":
            raise ControlPanelValidationError("LIVE_ONLY must remain the default mode")
        mode_payloads = _object(raw_modes, "dependence_modes")
        expected_mode_names = (
            {"LIVE_ONLY", "LIVE_PLUS_BACKTEST"}
            if globally_hybrid_eligible
            else {"LIVE_ONLY"}
        )
        if set(mode_payloads) != expected_mode_names:
            raise ControlPanelValidationError("dependence mode set differs")
        live_payload = _object(mode_payloads["LIVE_ONLY"], "dependence_modes.LIVE_ONLY")
        if live_payload != _object(correlation.get("dependence"), "dependence"):
            raise ControlPanelValidationError(
                "default dependence payload differs from LIVE_ONLY"
            )
        accepted_mode_names = tuple(mode_payloads)
        dependence_modes = {
            mode: _dependence_analysis(
                correlation,
                correlation_identity,
                dependence_strategy_names,
                dependence_payload=_object(
                    mode_payloads[mode], f"dependence_modes.{mode}"
                ),
                evidence_mode=mode,
                economic_contract_snapshot=economic_driver_snapshot,
            )
            for mode in accepted_mode_names
        }
        if "LIVE_PLUS_BACKTEST" in dependence_modes:
            eligible_versions = {
                _text(item.get("strategy_name"), "eligible strategy_name"): _text(
                    item.get("version_ref"), "eligible version_ref"
                )
                for item in _objects(
                    hybrid_mode_gate.get("eligible_strategies"),
                    "eligible strategies",
                )
            }
            blocked_names = {
                _text(item.get("strategy_name"), "blocked strategy_name")
                for item in _objects(
                    hybrid_mode_gate.get("blocked_strategies"),
                    "blocked strategies",
                )
            }
            hybrid_composition = _object(
                dependence_modes["LIVE_PLUS_BACKTEST"].get("series_composition"),
                "hybrid dependence series composition",
            )
            if set(hybrid_composition) != dependence_strategy_names:
                raise ControlPanelValidationError(
                    "hybrid dependence strategy boundary differs from registry gate"
                )
            for strategy_name, expected_version in eligible_versions.items():
                row = _object(
                    hybrid_composition.get(strategy_name),
                    f"hybrid composition {strategy_name}",
                )
                if row.get("strategy_version") != expected_version:
                    raise ControlPanelValidationError(
                        "eligible dependence hybrid version differs from registry: "
                        f"{strategy_name}"
                    )
            for strategy_name in blocked_names:
                row = _object(
                    hybrid_composition.get(strategy_name),
                    f"hybrid composition {strategy_name}",
                )
                if (
                    _finite_float(
                        row.get("backtest_observations_used", 0),
                        f"hybrid composition {strategy_name} backtest observations",
                    )
                    != 0
                ):
                    raise ControlPanelValidationError(
                        "unresolved strategy uses backtest history in hybrid dependence: "
                        f"{strategy_name}"
                    )
        dependence_analysis = dependence_modes[default_dependence_mode]
    canonical_strategy_series = _canonical_strategy_series_contracts(
        evidence_by_strategy,
        strategy_names,
        factor_analysis,
        dependence_analysis,
        accepted_weekly_returns,
        _objects(report.get("strategy_returns", []), "report strategy returns"),
        dependence_strategy_names,
    )
    freshness_dates = {
        _text(item.get("evaluated_on"), "freshness evaluated_on")
        for item in freshness_health
    }
    if len(freshness_dates) != 1:
        raise ControlPanelValidationError("freshness evaluation date is ambiguous")
    hwm_policy, hwm_policy_hash = _hwm_policy(
        date.fromisoformat(next(iter(freshness_dates))), hwm_policy_snapshot
    )
    effective_hwm_policy = _effective_hwm_policy_for_current_strategies(
        strategies, hwm_policy
    )
    strategies, hwm_summary = _governed_hwm_coverage(
        strategies,
        effective_hwm_policy,
        freshness_health,
        require_freshness=True,
        # The same common end date the report truncated every series to, so
        # the panel qualifies the perimeter exactly as the report did.
        calculation_boundary=(
            str(report["calculation_cutoff_date"])
            if report.get("calculation_cutoff_date") is not None
            else None
        ),
    )
    # Governed authority: the retained baseline named by the report is the
    # source, while owner controls independently project its seven-strategy
    # current-product HWM/return perimeter before report parity is admitted.
    if _text(report.get("as_of_date"), "report.as_of_date") != _text(
        baseline_catalog.get("as_of_date"), "catalog.as_of_date"
    ):
        raise ControlPanelValidationError(
            "report as_of_date differs from the governed baseline catalog"
        )
    report_perimeter = {
        _text(item["strategy_id"], "allocation_history.strategy_id")
        for item in allocations
        if item.get("scope") == "SYSTEMATIC" and item.get("strategy_id")
    }
    catalog_perimeter = {
        _text(item["strategy_id"], "catalog allocation.strategy_id")
        for item in _objects(current_product_catalog.get("allocations"), "allocations")
        if item.get("scope") == "SYSTEMATIC"
        and item.get("strategy_id")
        and strategy_product_controls.get(str(item["strategy_id"]), {}).get(
            "product_requirement"
        )
        != "EXCLUDED"
    }
    if report_perimeter != catalog_perimeter:
        raise ControlPanelValidationError(
            "report perimeter differs from the governed baseline catalog"
        )
    # Independently recompute the numerical composite from the governed
    # catalog (never the report's copies); the full-summary equality check
    # below then reconciles the numerical HWM fields against the report.
    portable_bundle_presence = {
        hwm_portable_receipt is not None,
        hwm_portable_generation_bytes is not None,
        hwm_portable_calendar_bytes is not None,
    }
    if len(portable_bundle_presence) != 1:
        raise ControlPanelValidationError(
            "portable HWM receipt, input generation, and calendar bytes must be supplied together"
        )
    portable_hwm_proof = (
        _panel_portable_hwm_proof(
            hwm_portable_receipt,
            hwm_portable_generation_bytes,
            hwm_portable_calendar_bytes,
            policy_bytes=portable_hwm_policy_snapshot,
            product_contract_bytes=financial_contract_snapshot,
            performance_policy_bytes=portable_hwm_performance_policy_snapshot,
            legacy_policy_bytes=hwm_policy_snapshot,
        )
        if hwm_portable_receipt is not None
        and hwm_portable_generation_bytes is not None
        and hwm_portable_calendar_bytes is not None
        else None
    )
    report_portable_hwm_proof = report.get("hwm_portable_proof")
    if report_portable_hwm_proof is not None and portable_hwm_proof is None:
        raise ControlPanelValidationError(
            "portable HWM authority is not supplied independently"
        )
    if portable_hwm_proof is not None:
        if report_portable_hwm_proof is None:
            raise ControlPanelValidationError(
                "accepted portfolio report lacks the portable HWM proof"
            )
        if (
            _object(report_portable_hwm_proof, "report.hwm_portable_proof")
            != portable_hwm_proof
        ):
            raise ControlPanelValidationError(
                "portable HWM proof differs from the accepted portfolio report"
            )
    # Governed HWM lane (#324/#370): same all-or-nothing bundle + independent
    # verification + reconciliation against the accepted report.
    governed_bundle_presence = {
        hwm_governed_result is not None,
        hwm_governed_generation_bytes is not None,
        hwm_governed_calendar_bytes is not None,
        hwm_governed_receipt_bytes is not None,
    }
    if len(governed_bundle_presence) != 1:
        raise ControlPanelValidationError(
            "governed HWM result, generation, calendar, and receipt bytes must be supplied together"
        )
    governed_input_contract = (
        financial_input_contract_snapshot
        if financial_input_contract_snapshot is not None
        else financial_contract_snapshot
    )
    governed_policy = portable_hwm_policy_snapshot
    if hwm_governed_result is not None:
        governed_input_contract = (
            _PORTABLE_HWM_PRODUCT_CONTRACT.read_bytes()
            if governed_input_contract is None
            else governed_input_contract
        )
        try:
            governed_policy = governed_hwm_input_policy(
                _PORTABLE_HWM_POLICY.read_bytes()
                if governed_policy is None
                else governed_policy,
                governed_input_contract,
                hwm_governed_result.get("policy_sha256", ""),
            )
        except CapitalValidationError as exc:
            raise ControlPanelValidationError(
                f"governed HWM result is invalid: {exc}"
            ) from exc
    governed_hwm_proof = (
        _panel_governed_hwm_proof(
            hwm_governed_result,
            hwm_governed_generation_bytes or b"",
            hwm_governed_calendar_bytes or b"",
            hwm_governed_receipt_bytes or b"",
            policy_bytes=governed_policy,
            product_contract_bytes=governed_input_contract,
            performance_policy_bytes=portable_hwm_performance_policy_snapshot,
            legacy_policy_bytes=hwm_policy_snapshot,
        )
        if hwm_governed_result is not None and hwm_governed_generation_bytes is not None
        else None
    )
    report_governed_hwm_proof = report.get("hwm_governed_proof")
    if report_governed_hwm_proof is not None and governed_hwm_proof is None:
        raise ControlPanelValidationError(
            "governed HWM authority is not supplied independently"
        )
    if governed_hwm_proof is not None:
        if report_governed_hwm_proof is None:
            raise ControlPanelValidationError(
                "accepted portfolio report lacks the governed HWM proof"
            )
        if (
            _object(report_governed_hwm_proof, "report.hwm_governed_proof")
            != governed_hwm_proof
        ):
            raise ControlPanelValidationError(
                "governed HWM proof differs from the accepted portfolio report"
            )
    hwm_numerical = _compute_panel_numerical_hwm(
        current_product_catalog, epoch_hpr_receipts
    )
    # The coverage rows inherit the current cutoff values and weights of the strategy
    # rows; the summary says which weight set it shows (the report says the same).
    hwm_summary = {
        **hwm_summary,
        **hwm_numerical,
        "weight_basis": WEIGHT_BASIS_CURRENT,
    }
    report_hwm = report.get("hwm_summary")
    if report_hwm is None:
        raise ControlPanelValidationError(
            "accepted portfolio report lacks the governed HWM summary"
        )
    if _object(report_hwm, "report.hwm_summary") != hwm_summary:
        raise ControlPanelValidationError(
            "governed HWM summary differs from the accepted portfolio report"
        )
    reported_hwm = _summary_value(summary, "Full-systematic HWM")
    if reported_hwm != hwm_summary["status"]:
        raise ControlPanelValidationError(
            "portfolio summary HWM status differs from governed HWM evidence"
        )
    # Readiness is derived from the recomputed numerical state: an
    # UNAVAILABLE composite must gate the report's advisory readiness to
    # DO_NOT_EXPAND_SYSTEMATIC_RISK; a permissive report row is rejected.
    report_readiness = _summary_value(summary, "Advisory readiness")
    if (
        hwm_summary.get("hwm_numerical_status") == "UNAVAILABLE"
        and report_readiness != "DO_NOT_EXPAND_SYSTEMATIC_RISK"
    ):
        raise ControlPanelValidationError(
            "report advisory readiness ignores the numerical HWM gate"
        )
    if not performance_lineage_available:
        strategies = _suppress_unmatched_strategy_performance(strategies)
        # No governed HWM/readiness or proof can be emitted as accepted when
        # its report/baseline performance lineage was rejected.  Keep the
        # policy identity but replace every performance-derived quantity.
        hwm_summary = _unavailable_lineage_hwm_summary(hwm_policy)
        portable_hwm_proof = None
        governed_hwm_proof = None
    strategies = [
        {
            **row,
            **(
                {"provider_evidence": evidence_by_strategy[str(row["strategy_id"])]}
                if str(row["strategy_id"]) in evidence_by_strategy
                else {}
            ),
        }
        for row in strategies
    ]
    capital_problems = _capital_identity_problems(
        {**capital_parts, "strategies": strategies}
    )
    if capital_problems:
        raise ControlPanelValidationError(
            "panel capital identity differs: " + "; ".join(capital_problems)
        )
    strategy_underwriting, underwriting_policy_hash = _strategy_underwriting(
        strategies,
        accepted_decomposition_lenses,
        dependence_analysis,
        accepted_weekly_returns,
        _text(report.get("as_of_date"), "report.as_of_date"),
        underwriting_policy_snapshot,
        performance_lineage_available=performance_lineage_available,
        implementation_contracts={
            key: (report[key] if isinstance(report.get(key), Mapping) else None)
            for _kind, key in _IMPLEMENTATION_REFERENCE_KEYS
        },
    )
    admitted_nav_names = sorted(
        _text(row.get("strategy_name"), "provider NAV strategy_name")
        for row in strategies
        if row.get("hwm_source") == "PORTFOLIO_REPORT"
        and row.get("nav_status") == "AVAILABLE"
        and row.get("hwm_freshness_status") == "ACCEPTED"
        and row.get("nav_evidence_layer") in {"ACCOUNT_LIVE", "PROVIDER_LIVE"}
    )
    coverage = _reconcile_hwm_coverage(coverage, hwm_summary)
    normalized_allocations = []
    for row in allocations:
        strategy_id = row.get("strategy_id")
        normalized_allocations.append(
            {
                **row,
                "strategy_name": (
                    "Zero-risk reserve"
                    if strategy_id is None
                    else strategy_names.get(str(strategy_id), "UNMAPPED")
                ),
            }
        )
    normalized_performance = [
        {
            **row,
            "strategy_name": (
                None
                if row.get("strategy_id") is None
                else strategy_names.get(str(row.get("strategy_id")), "UNMAPPED")
            ),
        }
        for row in performance
    ]
    classified_positions, position_snapshots = _classify_positions(
        positions, _position_cutoff_date(report)
    )
    normalized_positions = []
    identity_index = _holdings_identity_index(report)
    for row in classified_positions:
        symbol_status, symbol_reason, symbol, asset_category = (
            _position_symbol_status_and_reason(row, identity_index)
        )
        normalized_positions.append(
            {
                **row,
                "asset_category": asset_category,
                "symbol": symbol,
                "strategy_name": (
                    None
                    if row.get("strategy_id") is None
                    else strategy_names.get(str(row.get("strategy_id")), "UNMAPPED")
                ),
                "symbol_status": symbol_status,
                "symbol_reason": symbol_reason,
            }
        )

    fact_by_item = {
        str(row.get("item")): row
        for row in coverage
        if row.get("category") == "FACT_COVERAGE"
    }
    gates = []
    for item, label in (
        ("assignments", "Account/strategy mapping"),
        ("nav", "Daily NAV"),
        ("flows", "Cash flows"),
        ("positions", "Positions"),
        ("fx", "FX observations"),
    ):
        fact = fact_by_item.get(item)
        if fact is None:
            raise ControlPanelValidationError(f"coverage lacks {item}")
        gates.append(
            {
                "item": item,
                "label": label,
                "note": fact.get("known_gap")
                or "Accepted within the declared boundary",
                "status": fact.get("status"),
            }
        )
    blocking_items = [
        dict(row)
        for row in coverage
        if (
            row.get("category") == "RECONCILIATION_EXCEPTION"
            and row.get("status") != "PASS"
        )
        or (
            row.get("category") == "DATA_FRESHNESS"
            and row.get("escalation_required") is True
        )
    ]
    providers = _objects(legacy.get("providers"), "legacy.providers")
    allowed_candidate_fields = (
        "candidate",
        "ticker",
        "category",
        "tier",
        "portfolio_role",
        "philosophy_module",
        "execution",
        "cadence",
        "principal_gap",
    )
    frozen_providers = [
        {field: row.get(field) for field in allowed_candidate_fields}
        for row in providers
    ]
    tier_counts = Counter(str(row.get("tier")) for row in frozen_providers)
    provider_counts = Counter(
        _text(row.get("provider"), "account.provider") for row in accounts
    )
    twr_available = sum(
        str(row.get("twr_status", "")).startswith("AVAILABLE") for row in performance
    )
    mwr_available = sum(
        str(row.get("mwr_status", "")).startswith("AVAILABLE") for row in performance
    )
    # Current holdings only: the history stays published under its own label.
    position_count = position_snapshots["current_at_cutoff_rows"]
    reconciliation_passed = _reconciliation_passed(reconciliations)
    source_contract = _object(source.get("contract"), "source.contract")
    appendix = _object(source_contract.get("frozen_appendix"), "frozen_appendix")
    baseline = _object(report.get("baseline"), "baseline")
    template_hashes = (
        dict(template_hash_snapshots)
        if template_hash_snapshots is not None
        else {
            name: hashlib.sha256(_template_bytes(template_root / name)).hexdigest()
            for name in ("index.html", "analytics.css", "app.js")
        }
    )
    provenance = _objects(report.get("provenance"), "provenance")
    provenance = [
        {
            "artifact": "portfolio-report-generation",
            "as_of_date": report.get("as_of_date"),
            "schema": f"portfolio-report-data/{report['report_schema_version']}",
            "sha256": report_identity["sha256"],
        },
        *provenance,
        {
            "artifact": "frozen-candidate-appendix",
            "as_of_date": appendix.get("as_of_date"),
            "schema": "strategy-allocation-audit/frozen",
            "sha256": appendix.get("data_sha256"),
        },
        {
            "artifact": "portfolio-factor-decomposition-generation",
            "as_of_date": factor_analysis["as_of_date"],
            "schema": "portfolio-factor-decomposition/1",
            "sha256": decomposition_identity["sha256"],
        },
        *[
            {
                "artifact": "portfolio-decomposition-lens-"
                + _text(source_row.get("source_key"), "lens source_key"),
                "as_of_date": _text(source_row.get("as_of_date"), "lens as_of_date"),
                "schema": "portfolio-factor-decomposition/1",
                "sha256": _text(source_row.get("source_sha256"), "lens source_sha256"),
            }
            for lens in _objects(
                accepted_decomposition_lenses.get("lenses"),
                "accepted decomposition lenses",
            )
            if lens.get("lens_id") != "DYNAMIC_STYLE"
            for source_row in _objects(lens.get("sources"), "lens sources")
        ],
        *(
            [
                {
                    "artifact": "portfolio-weekly-return-generation",
                    "as_of_date": accepted_weekly_returns["as_of_date"],
                    "schema": "portfolio-weekly-returns/2",
                    "sha256": accepted_weekly_returns["source_sha256"],
                }
            ]
            if weekly_returns is not None
            else []
        ),
        *(
            [
                {
                    "artifact": "portfolio-candidate-driver-map",
                    "as_of_date": candidate_driver_map_identity["as_of_date"],
                    "schema": "portfolio-driver-map/1",
                    "sha256": candidate_driver_map_identity["sha256"],
                }
            ]
            if candidate_driver_map_identity is not None
            else []
        ),
        *(
            [
                {
                    "artifact": "portfolio-dynamic-research-replication",
                    "as_of_date": accepted_replication["as_of_date"],
                    "schema": "portfolio-dynamic-replication/1",
                    "sha256": accepted_replication["source_sha256"],
                }
            ]
            if replication_payload is not None
            else []
        ),
        {
            "artifact": "portfolio-correlation-generation",
            "as_of_date": dependence_analysis["as_of_date"],
            "schema": "portfolio-correlation/1",
            "sha256": correlation_identity["sha256"],
        },
        {
            "artifact": "portfolio-economic-driver-clusters",
            "as_of_date": _text(
                _load_json(
                    ECONOMIC_DRIVER_CLUSTERS,
                    "economic driver cluster contract",
                ).get("as_of_date"),
                "economic driver cluster as_of_date",
            ),
            "schema": "portfolio-economic-driver-clusters/1",
            "sha256": _hash(ECONOMIC_DRIVER_CLUSTERS),
        },
        {
            "artifact": "portfolio-strategy-underwriting-policy",
            "as_of_date": _text(
                _load_json(
                    STRATEGY_UNDERWRITING_POLICY,
                    "strategy underwriting policy",
                ).get("as_of_date"),
                "strategy underwriting policy as_of_date",
            ),
            "schema": "portfolio-strategy-underwriting-policy/1",
            "sha256": underwriting_policy_hash,
        },
        {
            "artifact": "portfolio-hwm-policy",
            "as_of_date": hwm_policy["evaluation_date"],
            "schema": "portfolio-hwm-policy/2",
            "sha256": hwm_policy_hash,
        },
        *(
            [
                {
                    "artifact": (
                        "canonical-report-strategy-provider-evidence"
                        if evidence_identity["source"] == "CANONICAL_PORTFOLIO_REPORT"
                        else "current-strategy-provider-evidence"
                    ),
                    "as_of_date": evidence_identity["as_of_date"],
                    "schema": (
                        "portfolio-report-data/strategy-provider-evidence/1"
                        if evidence_identity["source"] == "CANONICAL_PORTFOLIO_REPORT"
                        else "current-strategy-provider-evidence/1"
                    ),
                    "sha256": evidence_identity["sha256"],
                }
            ]
            if evidence_identity is not None
            else []
        ),
    ]
    if performance_lineage_available and not correlation_projection_applied:
        report_stress = _finite_float(
            _summary_value(summary, "Correlation-one stress"),
            "portfolio_summary.Correlation-one stress",
        )
        analytics_stress = _finite_float(
            _object(dependence_analysis["correlation_one"], "correlation_one").get(
                "loss"
            ),
            "correlation_one.loss",
        )
        if abs(report_stress - analytics_stress) > 1e-10:
            raise ControlPanelValidationError(
                "correlation-one stress differs from the accepted portfolio report"
            )
    source_clock = report.get("source_clock")
    source_clock_lines = (
        _source_clock_display_lines(source_clock) if source_clock is not None else []
    )
    if source_clock is not None and source_clock.get(
        "selected_financial_cutoff"
    ) != report.get("calculation_cutoff_date"):
        raise ControlPanelValidationError(
            "source clock selected financial cutoff differs from report cutoff"
        )
    limitations = sorted(
        {
            "Read-only local artifact; no provider, broker, order, or deployment connection exists.",
            "The candidate view is a frozen audit appendix and is not strategy, selection, or allocation authority.",
            (
                "Governed systematic HWM is unavailable because the report/baseline "
                "provider-ledger lineage differs from the verified epoch manifest."
                if not performance_lineage_available
                else (
                    "Governed systematic HWM remains unavailable for explicitly named "
                    "included-perimeter sleeves that are not HWM-qualified: "
                    f"{', '.join(hwm_summary['uncovered_strategies'])}."
                    if hwm_summary["uncovered_strategies"]
                    else "Governed systematic HWM has complete qualified NAV coverage."
                )
            ),
            (
                "Positions remain partial and FX remains unavailable; the panel does not infer either."
                if position_count
                else "Positions and FX remain unavailable; the panel does not infer either."
            ),
            "MWR is annualized ACTUAL_365 and remains unavailable unless its declared valuation perimeter and complete dated source-bound flows are present.",
            *(
                [
                    "Report-derived performance is unavailable because its provider-ledger lineage differs from the verified epoch manifest."
                ]
                if performance_lineage["status"]
                == "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH"
                else []
            ),
            "Provider, account-live, predecessor, shadow, and backtest evidence are not concatenated.",
            *(str(row.get("known_gap")) for row in coverage if row.get("known_gap")),
            *source_clock_lines,
            *(
                f"{item['item']}: {item['value']}"
                for item in report.get("methodology", [])
                if isinstance(item, Mapping)
                and item.get("item") in sleeve_disclosures.ITEM_NAMES
                and isinstance(item.get("value"), str)
            ),
        }
    )
    account_manifest_verification = (
        dict(account_manifest_identity["verification"])
        if account_manifest_identity is not None
        else {
            "status": "NOT_SUPPLIED",
            "method": "NONE",
            "producer_readiness": None,
            "publication_readiness": "NOT_READY",
        }
    )
    if (
        account_manifest_identity is not None
        and account_manifest_identity.get("authority_profile")
        == MANIFEST_AUTHORITY_VERIFIED_BUNDLE
    ):
        account_manifest_verification["epoch_manifest_source"] = (
            account_manifest_identity["epoch_manifest_source"]
        )

    panel_data = {
        "account_summary": accounts,
        "advisory": {
            "central_case_loss": (
                dependence_analysis["central_case"].get("loss")
                if performance_lineage_available
                else None
            ),
            "correlation_one_stress": (
                dependence_analysis["correlation_one"].get("loss")
                if performance_lineage_available
                else None
            ),
            "hwm_limit": "0.15",
            "hwm_status": hwm_summary["status"],
            "hwm_status_code_version": hwm_summary["status_code_version"],
            "hwm_status_semantics": hwm_summary["status_semantics"],
            "hwm_numerical_status": hwm_summary.get(
                "hwm_numerical_status", "UNAVAILABLE"
            ),
            **(
                {"hwm_numerical_reason": hwm_summary["hwm_numerical_reason"]}
                if hwm_summary.get("hwm_numerical_status", "UNAVAILABLE")
                == "UNAVAILABLE"
                else {}
            ),
            **(
                {
                    "hwm_index": hwm_summary["hwm_index"],
                    "hwm_current_drawdown": hwm_summary["hwm_current_drawdown"],
                    "hwm_maximum_drawdown": hwm_summary["hwm_maximum_drawdown"],
                }
                if hwm_summary.get("hwm_numerical_status") == "AVAILABLE"
                else {}
            ),
            "hwm_candidate": _hwm_candidate(governed_hwm_proof, hwm_summary),
            "nav_coverage": hwm_summary["nav_coverage"],
            "readiness": (
                _summary_value(summary, "Advisory readiness")
                if performance_lineage_available
                else "DO_NOT_EXPAND_SYSTEMATIC_RISK"
            ),
            "readiness_loss_measure": "CORRELATION_ONE_CONSERVATIVE_BOUND",
            "reason": (
                "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH: no report or baseline "
                "performance, HWM, or readiness conclusion is accepted."
                if not performance_lineage_available
                else (
                    "Readiness is governed by the correlation-one conservative bound; "
                    "the measured-dependence central case is descriptive and does not govern readiness. "
                    "Canonical report admitted NAV now covers "
                    + (", ".join(admitted_nav_names) or "no included-perimeter sleeves")
                    + "; explicit de-minimis exclusions remain reported: "
                    + (
                        ", ".join(
                            f"{item['strategy_name']} (${item['current_value_usd']}; {item['reason']})"
                            for item in hwm_summary.get("excluded_details", [])
                        )
                        or "none"
                    )
                    + ". "
                    + (
                        "Remaining HWM qualification gap: "
                        + ", ".join(
                            f"{item['strategy_name']} ({item['qualification_gap']})"
                            for item in hwm_summary["uncovered_details"]
                        )
                        + "."
                        if hwm_summary["uncovered_strategies"]
                        else "Every included HWM-perimeter sleeve has qualified governed account-live or provider-live return evidence."
                    )
                )
            ),
        },
        "allocations": normalized_allocations,
        "blocking_items": blocking_items,
        "candidate_appendix": {
            "as_of_date": appendix.get("as_of_date"),
            "providers": frozen_providers,
            "source_sha256": appendix.get("data_sha256"),
            "status": appendix.get("status"),
            "tier_counts": dict(sorted(tier_counts.items())),
        },
        "candidate_driver_map": (
            dict(candidate_driver_map) if candidate_driver_map is not None else None
        ),
        "account_manifest": (
            dict(account_manifest) if account_manifest is not None else None
        ),
        **(
            {
                "custody_current_capital": report_capital_expectations[
                    "custody_current_capital"
                ]
            }
            if "custody_current_capital" in report_capital_expectations
            else {}
        ),
        "capital": capital_parts["capital"],
        "capital_as_of": capital_parts["capital_as_of"],
        "capital_status": capital_parts["capital_status"],
        "counts": {
            "accounts": len(accounts),
            "mwr_available": mwr_available,
            "positions": position_count,
            "reconciliation_checks": len(reconciliations),
            "reconciliation_passed": reconciliation_passed,
            "strategies": len(strategies),
            "twr_available": twr_available,
            "twr_rows": len(performance),
        },
        "coverage": coverage,
        "fact_coverage_matrix": fact_coverage_matrix,
        "observation_windows": report.get("observation_windows", []),
        "attribution_dispositions": report.get("attribution_dispositions", []),
        "canonical_strategy_series": canonical_strategy_series,
        "default_dependence_mode": default_dependence_mode,
        "dependence_analysis": dependence_analysis,
        "dependence_modes": dependence_modes,
        "factor_analysis": factor_analysis,
        "decomposition_lenses": accepted_decomposition_lenses,
        "fact_status": {
            item: row.get("status") for item, row in sorted(fact_by_item.items())
        },
        "gates": gates,
        "limitations": limitations,
        "meta": {
            "baseline_generation_id": _text(
                baseline.get("generation_id"), "baseline.generation_id"
            ),
            "panel_mode": "LOCAL_DIRECT_FILE_READ_ONLY",
            "portfolio_as_of": _text(report.get("as_of_date"), "report.as_of_date"),
            "source_clock": report.get("source_clock"),
            "freshness_evaluated_on": freshness_evaluated_on,
            "performance_lineage": performance_lineage,
            "correlation_generation_id": correlation_identity["generation_id"],
            "correlation_sha256": correlation_identity["sha256"],
            "candidate_driver_map_sha256": (
                candidate_driver_map_identity["sha256"]
                if candidate_driver_map_identity is not None
                else None
            ),
            "account_manifest_sha256": (
                account_manifest_identity["sha256"]
                if account_manifest_identity is not None
                else None
            ),
            "account_manifest_size_bytes": (
                account_manifest_identity["size_bytes"]
                if account_manifest_identity is not None
                else None
            ),
            "account_manifest_authority_profile": (
                account_manifest_identity["authority_profile"]
                if account_manifest_identity is not None
                else MANIFEST_AUTHORITY_ABSENT
            ),
            "account_manifest_verification": account_manifest_verification,
            "decomposition_generation_id": decomposition_identity["generation_id"],
            "decomposition_sha256": decomposition_identity["sha256"],
            "decomposition_plan_bundle_id": accepted_decomposition_lenses.get(
                "plan_bundle_id"
            ),
            "decomposition_plan_manifest_sha256": accepted_decomposition_lenses.get(
                "plan_manifest_sha256"
            ),
            "hwm_policy_sha256": hwm_policy_hash,
            "hwm_status_code_version": HWM_STATUS_CODE_VERSION,
            "hwm_status_semantics": HWM_STATUS_SEMANTICS,
            "strategy_evidence_as_of": (
                evidence_identity["as_of_date"]
                if evidence_identity is not None
                else None
            ),
            "strategy_evidence_sha256": (
                evidence_identity["sha256"] if evidence_identity is not None else None
            ),
            "strategy_evidence_source": (
                evidence_identity["source"] if evidence_identity is not None else None
            ),
            "underwriting_policy_sha256": underwriting_policy_hash,
            "report_generation_id": report_identity["generation_id"],
            "report_sha256": report_identity["sha256"],
            "replication_generation_id": accepted_replication.get("generation_id"),
            "replication_sha256": accepted_replication.get("source_sha256"),
            "source_shell_hashes": source.get("observed_hashes"),
            "template_hashes": template_hashes,
            "weekly_returns_generation_id": accepted_weekly_returns.get(
                "generation_id"
            ),
            "weekly_returns_sha256": accepted_weekly_returns.get("source_sha256"),
        },
        "panel_schema_version": _panel_schema_version_for(
            report.get("report_schema_version")
        ),
        "performance": normalized_performance,
        "position_snapshots": position_snapshots,
        "positions": normalized_positions,
        "provenance": provenance,
        "provider_counts": dict(sorted(provider_counts.items())),
        "reconciliation_checks": reconciliations,
        "replication_analysis": accepted_replication,
        "strategies": strategies,
        "strategy_underwriting": strategy_underwriting,
        "hwm_summary": hwm_summary,
        "hybrid_mode_gate": hybrid_mode_gate,
        "leverage_disclosures": list(report["leverage_disclosures"]),
        "weekly_returns": accepted_weekly_returns,
    }
    if report.get("lifecycle_schema_version") == 1:
        # Lifecycle fields (#592): the current strategy rows already carry them
        # from the report; the retained history-only records follow them.  A
        # report without the marker is the previous schema and publishes the
        # previous shape.
        panel_data["lifecycle_schema_version"] = 1
        panel_data["strategies"] = [
            *strategies,
            *_objects(report.get("retained_strategies"), "retained_strategies"),
        ]
    if portable_hwm_proof is not None:
        panel_data["hwm_portable_proof"] = portable_hwm_proof
    if governed_hwm_proof is not None:
        panel_data["hwm_governed_proof"] = governed_hwm_proof
    return panel_data


def _financial_mvp_module() -> Any:
    """Load the repository financial source binder used at the panel boundary."""

    try:
        module = import_module("tools.portfolio_financial_mvp_contract")
    except ModuleNotFoundError as exc:  # direct `python tools/...` execution
        if exc.name != "tools":
            raise
        module = import_module("portfolio_financial_mvp_contract")
    expected = Path(__file__).resolve().parent / "portfolio_financial_mvp_contract.py"
    module_file = getattr(module, "__file__", None)
    if module_file is None or Path(module_file).resolve() != expected:
        raise ControlPanelValidationError(
            "financial MVP epoch-manifest binder origin differs"
        )
    return module


_EPOCH_MANIFEST_PATH: Final = "outputs/account-strategy-manifest.json"
_EPOCH_MANIFEST_REPORT_PATH: Final = "verification/independent-verification.json"
_EPOCH_MANIFEST_LEGACY_SUBJECT_PATHS: Final = (
    "authority/account-assignment-perimeter.json",
    "authority/account-flow-coverage.json",
    "authority/calculation-inputs.json",
    "authority/classified-flows.json",
    "authority/code-identities.json",
    "authority/flow-classification-policy.json",
    "authority/mapped-epoch-admission.json",
    "authority/selected-input-identities.json",
    _EPOCH_MANIFEST_PATH,
)
_EPOCH_MANIFEST_RECEIPT_AUTHORITY_PATH: Final = (
    "authority/account-interval-retrieval-receipt-authority.json"
)
_EPOCH_MANIFEST_ACCOUNT_ASSIGNMENT_CROSSWALK_PATH: Final = (
    "authority/account-assignment-crosswalk.json"
)
_SUPPORTED_EPOCH_MANIFEST_SCHEMA_VERSIONS: Final = frozenset({8, 9, 10})


def _epoch_manifest_source_from_bundle(
    bundle: GenerationBundle, payload: bytes
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Consume and bind a supported epoch-manifest payload from one bundle.

    The producer currently emits schema 9, while schema 8 remains accepted
    for retained bundles.  The artifact metadata is read from the payload and
    bundle rather than asserted as a fixture-specific constant.
    """

    if type(bundle) is not GenerationBundle or type(payload) is not bytes:
        raise ControlPanelValidationError(
            "epoch manifest source requires exact generation bundle bytes"
        )
    if bundle.manifest.verification is not VerificationStatus.VERIFIED:
        raise ControlPanelValidationError(
            "epoch manifest source requires a verified bundle"
        )
    if (
        bundle.inventory.generation_id != bundle.manifest.generation_id
        or bundle.receipt.generation_id != bundle.manifest.generation_id
    ):
        raise ControlPanelValidationError("epoch manifest source generation IDs differ")
    artifact_matches = tuple(
        artifact
        for artifact in bundle.manifest.artifacts
        if artifact.artifact_id == _EPOCH_MANIFEST_PATH
    )
    if len(artifact_matches) != 1:
        raise ControlPanelValidationError(
            "epoch manifest source requires exactly one required artifact"
        )
    artifact = artifact_matches[0]
    if artifact.schema_name != "account-strategy-manifest":
        raise ControlPanelValidationError(
            "epoch manifest source artifact schema name differs"
        )
    try:
        schema_version = int(artifact.schema_version)
    except (TypeError, ValueError) as exc:
        raise ControlPanelValidationError(
            "epoch manifest source artifact schema version differs"
        ) from exc
    if schema_version not in _SUPPORTED_EPOCH_MANIFEST_SCHEMA_VERSIONS:
        raise ControlPanelValidationError(
            "epoch manifest source artifact schema version is unsupported"
        )
    if (
        hashlib.sha256(payload).hexdigest() != artifact.sha256
        or len(payload) != artifact.size_bytes
    ):
        raise ControlPanelValidationError(
            "epoch manifest source artifact bytes differ from manifest"
        )
    try:
        decoded = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ControlPanelValidationError(
            "account manifest is not strict JSON"
        ) from exc
    value = _object(decoded, "account manifest")
    try:
        validate_manifest_schema(value)
    except ManifestError as exc:
        raise ControlPanelValidationError(
            f"account manifest is invalid: {exc}"
        ) from exc
    if value.get("schema_version") != schema_version:
        raise ControlPanelValidationError(
            "epoch manifest source artifact schema version differs from payload"
        )
    source = {
        "generation_id": bundle.manifest.generation_id,
        "generation_manifest_sha256": bundle.receipt.manifest.sha256,
        "generation_receipt_sha256": hashlib.sha256(bundle.receipt_bytes).hexdigest(),
        "full_directory_inventory_sha256": bundle.full_directory_inventory_sha256,
        "artifact": {
            "path": _EPOCH_MANIFEST_PATH,
            "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
            "schema": {
                "name": artifact.schema_name,
                "version": artifact.schema_version,
            },
        },
    }
    # A schema-9 bundle carries the assignment crosswalk as a subject, and
    # portfolio_financial_mvp_contract.epoch_manifest_source_from_bundle -- the copy
    # the release builder records in the recipe -- includes it.  Omitting it here
    # made the panel attest to a narrower identity than the recipe states, which
    # portfolio_pipeline_materialize.py:2354 compares for equality.  Retained
    # schema-8 bundles carry no crosswalk and are unchanged.
    #
    # Record what the bundle carries rather than re-validating its completeness:
    # the contract binder already rejects a schema-9 bundle with no crosswalk, and
    # the equality comparison above still fails closed on a genuinely missing one.
    crosswalk_matches = (
        tuple(
            entry
            for entry in bundle.manifest.artifacts
            if entry.artifact_id == _EPOCH_MANIFEST_ACCOUNT_ASSIGNMENT_CROSSWALK_PATH
        )
        if schema_version in {9, 10}
        else ()
    )
    if len(crosswalk_matches) == 1:
        crosswalk = crosswalk_matches[0]
        source["crosswalk"] = {
            "path": _EPOCH_MANIFEST_ACCOUNT_ASSIGNMENT_CROSSWALK_PATH,
            "sha256": crosswalk.sha256,
            "size_bytes": crosswalk.size_bytes,
            "schema": {
                "name": crosswalk.schema_name,
                "version": crosswalk.schema_version,
            },
        }
    return value, source


def _validate_epoch_manifest_report_schema(report: Mapping[str, Any]) -> None:
    """Validate the detached independent report's published closed schema."""

    try:
        from jsonschema import (  # type: ignore[import-untyped]
            Draft202012Validator,
            FormatChecker,
        )
    except ImportError as exc:  # pragma: no cover - environment boundary
        raise ControlPanelValidationError(
            "jsonschema is required to validate the independent verification report"
        ) from exc
    schema = _load_json(
        ACCOUNT_MANIFEST_INDEPENDENT_VERIFICATION_SCHEMA,
        "account-manifest independent verification schema",
    )
    errors = sorted(
        Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        ).iter_errors(report),
        key=str,
    )
    if errors:
        location = ".".join(str(part) for part in errors[0].absolute_path)
        prefix = f" at {location}" if location else ""
        raise ControlPanelValidationError(
            "independent verification report violates formal schema"
            f"{prefix}: {errors[0].message}"
        )


def _epoch_manifest_report_attestation(
    bundle: GenerationBundle,
    manifest: Mapping[str, Any],
    manifest_bytes: bytes,
) -> dict[str, Any]:
    """Attest a verifier report to every exact subject byte in one bundle.

    This consumer does not replay the independent calculation.  It proves that
    the report claiming that calculation is closed, canonical, and bound to the
    exact subject bytes being exposed by the panel.
    """

    if type(bundle) is not GenerationBundle:
        raise ControlPanelValidationError(
            "independent verification report requires an exact generation bundle"
        )
    report_artifacts = tuple(
        artifact
        for artifact in bundle.manifest.artifacts
        if artifact.artifact_id == _EPOCH_MANIFEST_REPORT_PATH
    )
    if len(report_artifacts) != 1:
        raise ControlPanelValidationError(
            "independent verification report artifact count differs"
        )
    report_artifact = report_artifacts[0]
    data_as_of = (
        _panel_date(manifest.get("calculation_cutoff_date"), "account manifest cutoff")
        if manifest.get("schema_version") in {9, 10}
        else bundle.manifest.as_of_date
    )
    if (
        bundle.receipt.independent_report != report_artifact
        or report_artifact.schema_name != "account-manifest-independent-verification"
        or report_artifact.as_of_date != data_as_of
    ):
        raise ControlPanelValidationError(
            "independent verification report artifact identity differs"
        )
    report_bytes = bundle.payloads.get(_EPOCH_MANIFEST_REPORT_PATH)
    if (
        type(report_bytes) is not bytes
        or hashlib.sha256(report_bytes).hexdigest() != report_artifact.sha256
        or len(report_bytes) != report_artifact.size_bytes
    ):
        raise ControlPanelValidationError(
            "independent verification report bytes differ from receipt"
        )
    try:
        decoded = json.loads(
            report_bytes.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ControlPanelValidationError(
            "independent verification report is not strict JSON"
        ) from exc
    report = _object(decoded, "independent verification report")
    if canonical_json_bytes(report) != report_bytes:
        raise ControlPanelValidationError(
            "independent verification report is not canonical"
        )
    # The retained v8 producer emitted report schema 3 before the crosswalk
    # became a required report subject. Its exact subject and count semantics
    # are validated below against the v8 bundle, so do not apply the v9-only
    # formal prefix contract to that legacy report.
    if manifest.get("schema_version") != 8:
        _validate_epoch_manifest_report_schema(report)
    expected_keys = {
        "artifact_id",
        "as_of_date",
        "blocker_chains",
        "counts",
        "invariants",
        "numeric_verification",
        "schema_version",
        "selected_source_identities",
        "status",
        "subjects",
        "subjects_semantic_sha256",
        "verifier",
    }
    if set(report) != expected_keys:
        raise ControlPanelValidationError(
            "independent verification report root keys differ"
        )
    has_receipt_authority = "receipt_authority" in _object(
        _object(
            manifest.get("source_identities"),
            "account manifest source identities",
        ).get("flow_coverage"),
        "account manifest flow coverage identity",
    )
    expected_version = 3 if has_receipt_authority else 2
    if (
        report.get("schema_version") != expected_version
        or report_artifact.schema_version != str(expected_version)
        or report.get("artifact_id") != _EPOCH_MANIFEST_REPORT_PATH
        or report.get("status") != "VERIFIED"
        or report.get("as_of_date") != data_as_of.isoformat()
    ):
        raise ControlPanelValidationError(
            "independent verification report authority fields differ"
        )

    actual_subject_paths = set(bundle.payloads) - {_EPOCH_MANIFEST_REPORT_PATH}
    expected_subject_paths: tuple[str, ...]
    if expected_version == 2:
        expected_subject_paths = _EPOCH_MANIFEST_LEGACY_SUBJECT_PATHS
    else:
        fixed_paths: tuple[str, ...] = (
            *_EPOCH_MANIFEST_LEGACY_SUBJECT_PATHS,
            _EPOCH_MANIFEST_RECEIPT_AUTHORITY_PATH,
        )
        if manifest.get("schema_version") in {9, 10}:
            fixed_paths += (_EPOCH_MANIFEST_ACCOUNT_ASSIGNMENT_CROSSWALK_PATH,)
        receipt_paths = tuple(
            sorted(actual_subject_paths - set(fixed_paths), key=str.casefold)
        )
        if not 1 <= len(receipt_paths) <= 15:
            raise ControlPanelValidationError(
                "independent verification report receipt subject count differs"
            )
        expected_subject_paths = (*fixed_paths, *receipt_paths)
    if actual_subject_paths != set(expected_subject_paths):
        raise ControlPanelValidationError(
            "independent verification report subject set differs from bundle"
        )
    expected_subjects = [
        {
            "path": path,
            "sha256": hashlib.sha256(bundle.payloads[path]).hexdigest(),
            "size_bytes": len(bundle.payloads[path]),
        }
        for path in expected_subject_paths
    ]
    if report.get("subjects") != expected_subjects:
        raise ControlPanelValidationError(
            "independent verification report subjects differ from bundle bytes"
        )
    expected_subject_digest = hashlib.sha256(
        canonical_json_bytes({"subjects": expected_subjects})
    ).hexdigest()
    if report.get("subjects_semantic_sha256") != expected_subject_digest:
        raise ControlPanelValidationError(
            "independent verification report subject digest differs"
        )
    output_subject = expected_subjects[
        expected_subject_paths.index(_EPOCH_MANIFEST_PATH)
    ]
    output_artifacts = tuple(
        artifact
        for artifact in bundle.manifest.artifacts
        if artifact.artifact_id == _EPOCH_MANIFEST_PATH
    )
    if (
        len(output_artifacts) != 1
        or bundle.payloads.get(_EPOCH_MANIFEST_PATH) != manifest_bytes
        or output_subject["sha256"] != output_artifacts[0].sha256
        or output_subject["size_bytes"] != output_artifacts[0].size_bytes
    ):
        raise ControlPanelValidationError(
            "independent verification report account-manifest subject differs"
        )

    selected_bytes = bundle.payloads.get("authority/selected-input-identities.json")
    if type(selected_bytes) is not bytes:
        raise ControlPanelValidationError(
            "independent verification report selected identities are absent"
        )
    selected_identities = _json_from_bytes(
        selected_bytes,
        "account-manifest selected input identities",
    )
    if (
        canonical_json_bytes(selected_identities) != selected_bytes
        or report.get("selected_source_identities") != selected_identities
    ):
        raise ControlPanelValidationError(
            "independent verification report selected identities differ"
        )
    if expected_version == 3:
        selected_receipts = selected_identities.get("retrieval_receipts")
        if not isinstance(selected_receipts, list):
            raise ControlPanelValidationError(
                "independent verification report receipt identities differ"
            )
        selected_receipt_map: dict[str, dict[str, object]] = {}
        for item in selected_receipts:
            receipt = _object(item, "selected retrieval receipt identity")
            receipt_id = receipt.get("receipt_id")
            if not isinstance(receipt_id, str) or receipt_id in selected_receipt_map:
                raise ControlPanelValidationError(
                    "independent verification report receipt identities differ"
                )
            selected_receipt_map[receipt_id] = {
                "sha256": receipt.get("sha256"),
                "size_bytes": receipt.get("size_bytes"),
            }
        expected_receipt_map = {
            path: {
                "sha256": hashlib.sha256(bundle.payloads[path]).hexdigest(),
                "size_bytes": len(bundle.payloads[path]),
            }
            for path in receipt_paths
        }
        if selected_receipt_map != expected_receipt_map:
            raise ControlPanelValidationError(
                "independent verification report receipt identities differ"
            )
    code_sources = _object(
        selected_identities.get("code_sources"),
        "account-manifest selected code sources",
    )
    verifier_source = _object(
        code_sources.get("independent_verifier"),
        "account-manifest selected independent verifier",
    )
    expected_verifier = {
        "artifact_id": "tools/verify_account_manifest_generation.py",
        # The verifier is a code artifact dated by the inputs it read, not by the
        # calculation boundary.  portfolio_report.py:7597 binds it to the same
        # `selected_source_identities.as_of_date`; the two must agree.
        "as_of_date": _text(
            selected_identities.get("as_of_date"),
            "account-manifest selected identities as_of_date",
        ),
        "schema": {
            "name": "independent-account-manifest-verifier",
            "version": "1",
        },
        "sha256": verifier_source.get("sha256"),
        "size_bytes": verifier_source.get("size_bytes"),
    }
    if report.get("verifier") != expected_verifier:
        raise ControlPanelValidationError(
            "independent verification report verifier identity differs"
        )

    rows = _objects(manifest.get("rows"), "account manifest rows")
    row_by_assignment: dict[tuple[object, object, object, object], dict[str, Any]] = {}
    for row in rows:
        key = (
            row.get("account_id"),
            row.get("strategy_id"),
            row.get("effective_start"),
            row.get("effective_end"),
        )
        if key in row_by_assignment:
            raise ControlPanelValidationError(
                "account manifest report assignment is duplicated"
            )
        row_by_assignment[key] = row
    chains = _objects(report.get("blocker_chains"), "report blocker chains")
    seen_assignments: set[tuple[object, object, object, object]] = set()
    for chain in chains:
        assignment = _object(chain.get("assignment"), "report blocker assignment")
        key = (
            assignment.get("account_id"),
            assignment.get("strategy_id"),
            assignment.get("effective_start"),
            assignment.get("effective_end"),
        )
        manifest_row = row_by_assignment.get(key)
        if manifest_row is None or key in seen_assignments:
            raise ControlPanelValidationError(
                "independent verification report blocker assignment differs"
            )
        seen_assignments.add(key)
        output = _object(chain.get("output"), "report blocker output")
        is_numeric = manifest_row.get("status") == "OK"
        if (
            chain.get("numeric_candidate") is not is_numeric
            or output.get("status") != manifest_row.get("status")
            or output.get("blocking_reason") != manifest_row.get("blocking_reason")
        ):
            raise ControlPanelValidationError(
                "independent verification report row disposition differs"
            )
    if seen_assignments != set(row_by_assignment):
        raise ControlPanelValidationError(
            "independent verification report row coverage differs"
        )
    ok_count = sum(row.get("status") == "OK" for row in rows)
    numeric = _object(
        report.get("numeric_verification"),
        "independent verification report numeric verification",
    )
    expected_disposition = (
        "INDEPENDENT_RECALCULATION_NO_DISCREPANCIES"
        if ok_count
        else "NOT_APPLICABLE_NO_CANDIDATES"
    )
    if numeric != {
        "candidate_count": ok_count,
        "comparison_count": ok_count,
        "discrepancy_count": 0,
        "disposition": expected_disposition,
    }:
        raise ControlPanelValidationError(
            "independent verification report numeric result differs"
        )
    counts = _object(report.get("counts"), "independent verification report counts")
    if (
        counts.get("output_row_count") != len(rows)
        or counts.get("blocked_output_row_count") != len(rows) - ok_count
        or (expected_version == 3 and counts.get("ok_output_row_count") != ok_count)
        or (expected_version == 2 and ok_count != 0)
    ):
        raise ControlPanelValidationError(
            "independent verification report output counts differ"
        )
    if expected_version == 3:
        complete_coverage = sum(
            _object(chain.get("coverage"), "report chain coverage").get("status")
            == "COMPLETE"
            for chain in chains
        )
        funded_admission = sum(
            _object(chain.get("admission"), "report chain admission").get("status")
            == "FUNDED"
            for chain in chains
        )
        perimeter = _json_from_bytes(
            bundle.payloads["authority/account-assignment-perimeter.json"],
            "account-assignment perimeter",
        )
        partition_counts = _object(
            perimeter.get("partition_counts"),
            "account-assignment perimeter partition counts",
        )
        mapped_assignment_count = partition_counts.get("STRATEGY_MAPPED")
        if type(mapped_assignment_count) is not int or mapped_assignment_count < 0:
            raise ControlPanelValidationError(
                "account-assignment perimeter mapped count is invalid"
            )
        expected_count_values = {
            "mapped_assignment_count": mapped_assignment_count,
            "calculation_input_row_count": len(rows),
            "coverage_row_count": len(rows),
            "admission_row_count": len(rows),
            "output_row_count": len(rows),
            "complete_coverage_row_count": complete_coverage,
            "blocked_coverage_row_count": len(rows) - complete_coverage,
            "funded_admission_row_count": funded_admission,
            "blocked_admission_row_count": len(rows) - funded_admission,
            "ok_output_row_count": ok_count,
            "blocked_output_row_count": len(rows) - ok_count,
        }
        if any(
            counts.get(name) != expected
            for name, expected in expected_count_values.items()
        ):
            raise ControlPanelValidationError(
                "independent verification report coverage or admission counts differ"
            )

    return {
        "artifact_id": _EPOCH_MANIFEST_REPORT_PATH,
        "sha256": report_artifact.sha256,
        "size_bytes": report_artifact.size_bytes,
        "schema": {
            "name": report_artifact.schema_name,
            "version": report_artifact.schema_version,
        },
        "report_schema_version": expected_version,
        "status": "REPORT_ATTESTED_TO_EXACT_BUNDLE_BYTES",
        "subjects_semantic_sha256": expected_subject_digest,
        "numeric_verification": numeric,
    }


def _verified_bundle_account_manifest(
    staged_manifest_path: Path,
    generation_root: Path,
    generation_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read one exact verified bundle and bind its staged manifest bytes."""

    try:
        bundle = read_generation_bundle(generation_root, generation_id)
    except (
        GenerationConflictError,
        ManifestValidationError,
        OSError,
        TypeError,
    ) as exc:
        raise ControlPanelValidationError(
            "verified epoch-manifest generation bundle is invalid"
        ) from exc
    try:
        payload = bundle.payloads[_EPOCH_MANIFEST_PATH]
        value, source = _epoch_manifest_source_from_bundle(bundle, payload)
    except ControlPanelValidationError:
        raise
    except (
        AttributeError,
        ImportError,
        KeyError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise ControlPanelValidationError(
            "verified epoch-manifest generation bundle binding failed"
        ) from exc
    source = _object(source, "epoch manifest source")
    if type(payload) is not bytes:
        raise ControlPanelValidationError(
            "verified epoch-manifest generation payload must be exact bytes"
        )

    staged_bytes, _staged_sha256 = _read_snapshot(
        staged_manifest_path, "staged account manifest"
    )
    if staged_bytes != payload:
        raise ControlPanelValidationError(
            "staged account manifest differs from verified generation bundle"
        )
    # The payload was parsed and schema-validated from the same bytes above;
    # the staged copy is accepted only after exact byte equality.
    readiness = _object(value["authority_readiness"], "account manifest readiness")
    report_attestation = _epoch_manifest_report_attestation(
        bundle,
        value,
        staged_bytes,
    )
    current_epoch_crosswalk = None
    if value.get("schema_version") in {9, 10}:
        crosswalk_bytes = bundle.payloads.get(
            _EPOCH_MANIFEST_ACCOUNT_ASSIGNMENT_CROSSWALK_PATH
        )
        if type(crosswalk_bytes) is not bytes:
            raise ControlPanelValidationError(
                "current epoch crosswalk is absent from the verified bundle"
            )
        current_epoch_crosswalk = _json_from_bytes(
            crosswalk_bytes, "current epoch crosswalk"
        )
        _current_epoch_crosswalk_included_tuples(current_epoch_crosswalk)

    return value, {
        "authority_profile": MANIFEST_AUTHORITY_VERIFIED_BUNDLE,
        "sha256": hashlib.sha256(staged_bytes).hexdigest(),
        "size_bytes": len(staged_bytes),
        "epoch_manifest_source": source,
        "current_epoch_crosswalk": current_epoch_crosswalk,
        "verification": {
            "status": "REPORT_ATTESTED_TO_EXACT_BUNDLE_BYTES",
            "method": ("READ_GENERATION_BUNDLE_EXACT_BYTES_AND_FINANCIAL_MVP_BINDER"),
            "producer_readiness": readiness,
            "publication_readiness": "NOT_READY",
            "independent_report": report_attestation,
        },
    }


def _account_manifest(
    path: Path | None,
    *,
    financial_mode: bool,
    ledger_path: Path | None,
    ledger_identity_path: Path | None,
    baseline_root: Path | None,
    baseline_generation_id: str | None,
    classified_flows_path: Path | None,
    flow_coverage_path: Path | None,
    classification_policy_path: Path | None,
    admission_policy_path: Path | None,
    account_manifest_generation_root: Path | None = None,
    account_manifest_generation_id: str | None = None,
    membership_census_path: Path | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Bind one manifest snapshot and independently reproduce it when required.

    A manifest's embedded hashes are declarations, not authority.  Financial
    mode therefore accepts it only if the same current producer reconstructs
    the identical canonical object from separately selected governed inputs.
    """
    has_generation_root = account_manifest_generation_root is not None
    has_generation_id = account_manifest_generation_id is not None
    if has_generation_root != has_generation_id:
        raise ControlPanelValidationError(
            "account manifest generation root and generation id must be supplied together"
        )
    authority_paths = {
        "ledger": ledger_path,
        "ledger identity": ledger_identity_path,
        "baseline root": baseline_root,
        "baseline generation id": baseline_generation_id,
        "classified flows": classified_flows_path,
        "flow coverage": flow_coverage_path,
        "flow classification policy": classification_policy_path,
        "admission policy": admission_policy_path,
    }
    if has_generation_root:
        assert account_manifest_generation_root is not None
        assert account_manifest_generation_id is not None
        if (
            path is not None
            or financial_mode
            or any(candidate is not None for candidate in authority_paths.values())
        ):
            raise ControlPanelValidationError(
                "verified account-manifest generation cannot be combined with legacy account-manifest inputs"
            )
        bundle_manifest_path = (
            account_manifest_generation_root
            / "generations"
            / account_manifest_generation_id
            / _EPOCH_MANIFEST_PATH
        )
        return _verified_bundle_account_manifest(
            bundle_manifest_path,
            account_manifest_generation_root,
            account_manifest_generation_id,
        )
    if not financial_mode:
        if path is None:
            return None, None
    else:
        if path is None:
            raise ControlPanelValidationError(
                "financial mode requires an account manifest"
            )
        missing = [
            label for label, candidate in authority_paths.items() if candidate is None
        ]
        if missing:
            raise ControlPanelValidationError(
                "financial mode requires independently selected " + ", ".join(missing)
            )
        assert classification_policy_path is not None
        try:
            selected_policy = classification_policy_path.resolve(strict=True)
            canonical_policy = FLOW_CLASSIFICATION_POLICY.resolve(strict=True)
        except OSError as exc:
            raise ControlPanelValidationError(
                "financial flow classification policy is missing"
            ) from exc
        if (
            classification_policy_path.is_symlink()
            or selected_policy != canonical_policy
        ):
            raise ControlPanelValidationError(
                "financial mode requires the repository-governed flow classification policy"
            )
    assert path is not None
    try:
        manifest_bytes, _manifest_sha256 = _read_snapshot(path, "account manifest")
        value = json.loads(manifest_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControlPanelValidationError(
            "account manifest is not readable JSON"
        ) from exc
    value = _object(value, "account manifest")
    try:
        validate_manifest_schema(value)
    except ManifestError as exc:
        raise ControlPanelValidationError(
            f"account manifest is invalid: {exc}"
        ) from exc
    if financial_mode:
        assert ledger_path is not None
        assert ledger_identity_path is not None
        assert baseline_root is not None
        assert baseline_generation_id is not None
        assert classified_flows_path is not None
        assert flow_coverage_path is not None
        assert classification_policy_path is not None
        assert admission_policy_path is not None
        try:
            expected = build_manifest_candidate(
                ledger_path=ledger_path,
                ledger_identity_path=ledger_identity_path,
                baseline_root=baseline_root,
                baseline_generation_id=baseline_generation_id,
                classified_flows_path=classified_flows_path,
                flow_coverage_path=flow_coverage_path,
                classification_policy_path=classification_policy_path,
                admission_policy_path=admission_policy_path,
                membership_census_path=(
                    _GOVERNED_MEMBERSHIP_CENSUS
                    if membership_census_path is None
                    else membership_census_path
                ),
            )
        except ManifestError as exc:
            raise ControlPanelValidationError(
                f"independent account-manifest rebuild failed: {exc}"
            ) from exc
        if canonical_json_bytes(value) != canonical_json_bytes(expected):
            raise ControlPanelValidationError(
                "account manifest differs from independently rebuilt candidate"
            )
    return value, {
        "authority_profile": (
            MANIFEST_AUTHORITY_FINANCIAL
            if financial_mode
            else MANIFEST_AUTHORITY_INTERNAL
        ),
        "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "size_bytes": len(manifest_bytes),
        "verification": {
            "status": (
                "PANEL_INDEPENDENT_EXACT_REBUILD_VERIFIED"
                if financial_mode
                else "INTERNAL_SCHEMA_AND_SEMANTIC_VALIDATION_ONLY"
            ),
            "method": (
                "EXACT_REBUILD_REPORT_BASELINE_CANONICAL_POLICY_AND_TEMPLATE_BOUND"
                if financial_mode
                else "SELF_CONTAINED_SCHEMA_AND_DERIVATION_CHECKS"
            ),
            "producer_readiness": value["authority_readiness"],
            "publication_readiness": "READY" if financial_mode else "NOT_READY",
        },
    }


def _validate_panel_data_schema(
    data: Mapping[str, Any],
    panel_schema_snapshot: bytes | None = None,
    manifest_schema_snapshot: bytes | None = None,
) -> None:
    """Enforce the published panel contract, including manifest/profile binding."""
    try:
        from jsonschema import (
            Draft202012Validator,
            FormatChecker,
        )
        from referencing import Registry, Resource
    except ImportError as exc:  # pragma: no cover - environment boundary
        raise ControlPanelValidationError(
            "jsonschema is required to validate panel data"
        ) from exc
    schema = (
        _json_from_bytes(panel_schema_snapshot, "control-panel data schema")
        if panel_schema_snapshot is not None
        else _load_json(PANEL_DATA_SCHEMA, "control-panel data schema")
    )
    manifest_schema = (
        _json_from_bytes(manifest_schema_snapshot, "account-strategy manifest schema")
        if manifest_schema_snapshot is not None
        else _load_json(ACCOUNT_MANIFEST_SCHEMA, "account-strategy manifest schema")
    )
    manifest_schema_id = _text(
        manifest_schema.get("$id"), "account-strategy manifest schema $id"
    )
    registry = Registry().with_resource(
        manifest_schema_id, Resource.from_contents(manifest_schema)
    )
    errors = sorted(
        Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
            registry=registry,
        ).iter_errors(data),
        key=str,
    )
    if errors:
        location = ".".join(str(part) for part in errors[0].absolute_path)
        prefix = f" at {location}" if location else ""
        raise ControlPanelValidationError(
            f"panel data violates formal schema{prefix}: {errors[0].message}"
        )


def _rendered_files(
    report_root: Path | None,
    legacy_panel: Path,
    template_root: Path,
    decomposition_root: Path | None = None,
    correlation_root: Path | None = None,
    strategy_evidence_path: Path | None = None,
    decomposition_lenses_root: Path | None = None,
    decomposition_plan_bundle: Path | None = None,
    weekly_returns_root: Path | None = None,
    freshness_health_path: Path | None = None,
    replication_root: Path | None = None,
    candidate_driver_map_path: Path | None = None,
    baseline_root: Path | None = None,
    hwm_portable_receipt: Mapping[str, Any] | None = None,
    hwm_portable_generation_bytes: bytes | None = None,
    hwm_portable_calendar_bytes: bytes | None = None,
    hwm_governed_result: Mapping[str, Any] | None = None,
    hwm_governed_generation_bytes: bytes | None = None,
    hwm_governed_calendar_bytes: bytes | None = None,
    hwm_governed_receipt_bytes: bytes | None = None,
    account_manifest_path: Path | None = None,
    financial_mode: bool = False,
    manifest_ledger_path: Path | None = None,
    manifest_ledger_identity_path: Path | None = None,
    manifest_baseline_root: Path | None = None,
    manifest_classified_flows_path: Path | None = None,
    manifest_flow_coverage_path: Path | None = None,
    manifest_classification_policy_path: Path | None = None,
    manifest_admission_policy_path: Path | None = None,
    *,
    invested_capital_result: Mapping[str, Any] | None = None,
    invested_capital_input_generation_path: Path | None = None,
    report_candidate: Path | None = None,
    strategy_history_contract_path: Path | None = None,
    account_manifest_generation_root: Path | None = None,
    account_manifest_generation_id: str | None = None,
    manifest_membership_census_path: Path | None = None,
    hybrid_decomposition_plan_bundle: Path | None = None,
    dependence_selected_inputs_path: Path | None = None,
) -> tuple[
    dict[str, bytes],
    dict[str, Any],
    dict[str, tuple[Path, str]],
]:
    if financial_mode:
        try:
            selected_templates = template_root.resolve(strict=True)
            canonical_templates = DEFAULT_TEMPLATE_ROOT.resolve(strict=True)
        except OSError as exc:
            raise ControlPanelValidationError(
                "financial panel template root is missing"
            ) from exc
        if template_root.is_symlink() or selected_templates != canonical_templates:
            raise ControlPanelValidationError(
                "financial mode requires the repository-governed panel templates"
            )
    if (report_root is None) == (report_candidate is None):
        raise ControlPanelValidationError(
            "supply exactly one of report root or report candidate"
        )
    report_identity: Mapping[str, str | None]
    if report_candidate is None:
        assert report_root is not None
        report, report_identity, report_path = _current_report(report_root)
        verified_strategy_history: dict[str, Any] | None = None
        strategy_history_binding: tuple[Path, str] | None = None
        if strategy_history_contract_path is None:
            if report.get("strategy_history_contract") is not None:
                raise ControlPanelValidationError(
                    "published report declares a strategy history contract; "
                    "--strategy-history-contract is required for HPR HWM parity"
                )
        else:
            # The published report carries the same strategy_history_contract
            # identity as a candidate, and _compute_panel_numerical_hwm needs
            # that source's epoch HPR receipts to mirror the report's numerical
            # HWM.  The baseline projection never carries them, so without this
            # the published path reports NO_HWM_INPUTS while the report reports
            # MISSING_DAILY_POINT_CAN_HIDE_PEAK and HWM parity cannot hold for
            # any portfolio with a declared holding-period span.
            verified_strategy_history, strategy_history_binding = (
                _verified_strategy_history_contract(
                    strategy_history_contract_path,
                    _object(
                        report.get("strategy_history_contract"),
                        "report.strategy_history_contract",
                    ),
                    dependence_selected_inputs_path,
                )
            )
    else:
        report, report_identity, report_path = _report_candidate(report_candidate)
        if strategy_history_contract_path is None:
            raise ControlPanelValidationError(
                "report candidate requires a strategy history contract"
            )
        verified_strategy_history, strategy_history_binding = (
            _verified_strategy_history_contract(
                strategy_history_contract_path,
                _object(
                    report.get("strategy_history_contract"),
                    "report.strategy_history_contract",
                ),
                dependence_selected_inputs_path,
            )
        )
    report_identity_values = {
        "generation_id": _text(
            report_identity.get("generation_id"), "report generation_id"
        ),
        "sha256": _text(report_identity.get("sha256"), "report sha256"),
    }
    portfolio_as_of = _text(report.get("as_of_date"), "report.as_of_date")
    if freshness_health_path is None:
        raise ControlPanelValidationError("freshness health is mandatory")
    freshness_content, freshness_sha256 = _read_snapshot(
        freshness_health_path, "freshness health"
    )
    freshness_health = _freshness_health(
        freshness_health_path, portfolio_as_of, freshness_content
    )
    baseline_catalog, baseline_bindings = _baseline_catalog_snapshot_for_report(
        baseline_root, report
    )
    resolved_decomposition, resolved_correlation = _analytics_roots(
        report_root, decomposition_root, correlation_root
    )
    decomposition, decomposition_identity, decomposition_path = _current_analytics(
        resolved_decomposition,
        filename="decomposition.json",
        field_name="portfolio factor decomposition",
        expected_schema_version=(1, 2, 3),
    )
    correlation, correlation_identity, correlation_path = _current_analytics(
        resolved_correlation,
        filename="correlation.json",
        field_name="portfolio correlation",
        expected_schema_version=(1, 2),
    )
    if (
        hybrid_decomposition_plan_bundle is not None
        and decomposition_plan_bundle is None
    ):
        raise ControlPanelValidationError(
            "hybrid decomposition requires a live plan bundle"
        )
    if (decomposition_lenses_root is None) != (decomposition_plan_bundle is None):
        raise ControlPanelValidationError(
            "decomposition lens root and plan bundle must be supplied together"
        )
    financial_contract_snapshot = _read_snapshot(
        FINANCIAL_MVP_CONTRACT, "financial MVP contract"
    )
    financial_input_contract_path = FINANCIAL_MVP_CONTRACT
    financial_input_contract_bytes = financial_contract_snapshot[0]
    if invested_capital_input_generation_path is not None:
        financial_input_contract_path, financial_input_contract_bytes = (
            resolve_financial_input_authority(
                invested_capital_input_generation_path,
                FINANCIAL_MVP_CONTRACT,
                baseline_catalog,
            )
        )
    if (invested_capital_result is None) != (
        invested_capital_input_generation_path is None
    ):
        raise ControlPanelValidationError(
            "invested-capital result and input generation must be supplied together"
        )
    invested_capital = (
        dict(
            verify_governed_capital_result(
                invested_capital_result,
                invested_capital_input_generation_path,
                financial_input_contract_path,
            )
        )
        if invested_capital_result is not None
        and invested_capital_input_generation_path is not None
        else None
    )
    if (
        invested_capital is not None
        and report.get("invested_capital") != invested_capital
    ):
        raise ControlPanelValidationError(
            "panel invested-capital result differs from report authority"
        )
    _preflight_hybrid_gate, include_hybrid_sources = _report_hybrid_mode_gate(
        report, financial_contract_snapshot[0]
    )
    lens_payload: dict[str, Any] | None = None
    lens_bindings: tuple[tuple[Path, str], ...] = ()
    plan_strategy_names: dict[str, str] | None = None
    current_strategy_names_by_id = {
        _text(row.get("strategy_id"), "strategy.strategy_id"): _text(
            row.get("strategy_name"), "strategy.strategy_name"
        )
        for row in _current_product_rows(
            _objects(report.get("strategy_summary"), "strategy_summary"),
            _financial_mvp_product_controls(financial_contract_snapshot[0]),
        )
    }
    baseline_strategy_names = _baseline_correlation_strategy_mapping(
        baseline_catalog, current_strategy_names_by_id
    )
    if decomposition_lenses_root is not None and decomposition_plan_bundle is not None:
        lens_payload, lens_bindings = _decomposition_lenses(
            decomposition_lenses_root,
            decomposition_plan_bundle,
            current_strategy_names_by_id,
            financial_contract_snapshot[0],
            include_hybrid=include_hybrid_sources,
            hybrid_plan_bundle=hybrid_decomposition_plan_bundle,
        )
        # the weekly and correlation identity authorities are the product perimeter
        plan_strategy_names = _plan_without_retained_records(
            _decomposition_plan_strategy_mapping(decomposition_plan_bundle),
            financial_contract_snapshot[0],
        )
    report_lenses = report.get("decomposition_lenses")
    if report_lenses is not None and lens_payload is None:
        raise ControlPanelValidationError(
            "report decomposition authority requires explicit lens inputs"
        )
    if report_lenses is not None and report_lenses != lens_payload:
        raise ControlPanelValidationError(
            "panel decomposition projection differs from report authority"
        )
    weekly_payload: dict[str, Any] | None = None
    weekly_path: Path | None = None
    if weekly_returns_root is not None:
        weekly, weekly_identity, weekly_path = _current_analytics(
            weekly_returns_root,
            filename="weekly-returns.json",
            field_name="portfolio weekly returns",
            expected_schema_version=2,
        )
        weekly_payload = _weekly_return_analysis(
            weekly,
            weekly_identity,
            current_strategy_names_by_id,
            plan_strategy_names,
            include_hybrid=include_hybrid_sources,
        )
    replication_payload: dict[str, Any] | None = None
    replication_path: Path | None = None
    if replication_root is not None:
        replication, replication_identity, replication_path = _current_replication(
            replication_root
        )
        strategy_name_set = {
            _text(row.get("strategy_name"), "strategy.strategy_name")
            for row in _objects(report.get("strategy_summary"), "strategy_summary")
        }
        replication_payload = _replication_analysis(
            replication,
            replication_identity,
            strategy_name_set,
            decomposition_identity,
        )
    (
        candidate_driver_map,
        candidate_driver_map_identity,
        candidate_driver_map_content,
    ) = _candidate_driver_map_snapshot(candidate_driver_map_path)
    account_manifest, account_manifest_identity = _account_manifest(
        account_manifest_path,
        financial_mode=financial_mode,
        ledger_path=manifest_ledger_path,
        ledger_identity_path=manifest_ledger_identity_path,
        baseline_root=manifest_baseline_root,
        baseline_generation_id=(
            _text(
                _object(report.get("baseline"), "report baseline").get("generation_id"),
                "report baseline generation_id",
            )
            if financial_mode
            else None
        ),
        classified_flows_path=manifest_classified_flows_path,
        flow_coverage_path=manifest_flow_coverage_path,
        classification_policy_path=manifest_classification_policy_path,
        admission_policy_path=manifest_admission_policy_path,
        account_manifest_generation_root=account_manifest_generation_root,
        account_manifest_generation_id=account_manifest_generation_id,
        membership_census_path=manifest_membership_census_path,
    )
    if financial_mode:
        assert baseline_root is not None
        assert manifest_baseline_root is not None
        try:
            panel_baseline_root = baseline_root.resolve(strict=True)
            manifest_selected_baseline_root = manifest_baseline_root.resolve(
                strict=True
            )
        except OSError as exc:
            raise ControlPanelValidationError(
                "financial governed baseline root is missing"
            ) from exc
        if (
            baseline_root.is_symlink()
            or manifest_baseline_root.is_symlink()
            or panel_baseline_root != manifest_selected_baseline_root
        ):
            raise ControlPanelValidationError(
                "financial manifest baseline root differs from panel authority"
            )
        assert account_manifest is not None
        if account_manifest.get("accounting_currency") != "USD":
            raise ControlPanelValidationError(
                "financial manifest accounting currency must be USD"
            )
        manifest_sources = _object(
            account_manifest.get("source_identities"),
            "account manifest source identities",
        )
        source_as_of_dates = {
            label: _text(
                _object(
                    _object(manifest_sources.get(label), f"manifest {label}").get(
                        "artifact"
                    ),
                    f"manifest {label} artifact",
                ).get("as_of_date"),
                f"manifest {label} artifact as_of_date",
            )
            for label in (
                "ledger",
                "baseline",
                "classified_flows",
                "flow_coverage",
                "admission_policy",
            )
        }
        report_as_of = _text(report.get("as_of_date"), "report.as_of_date")
        if set(source_as_of_dates.values()) != {report_as_of}:
            raise ControlPanelValidationError(
                "financial manifest source dates differ from the report candidate: "
                f"report={report_as_of}, sources={source_as_of_dates}"
            )
        manifest_policy = _object(
            manifest_sources.get("flow_classification_policy"),
            "manifest flow classification policy",
        )
        manifest_policy_artifact = _object(
            manifest_policy.get("artifact"),
            "manifest flow classification policy artifact",
        )
        policy_as_of = _text(
            manifest_policy_artifact.get("as_of_date"),
            "manifest flow classification policy as_of_date",
        )
        if policy_as_of > report_as_of:
            raise ControlPanelValidationError(
                "financial manifest uses a future flow classification policy"
            )
        report_baseline = _object(report.get("baseline"), "report baseline")
        manifest_baseline = _object(
            manifest_sources.get("baseline"), "manifest baseline"
        )
        manifest_baseline_artifact = _object(
            manifest_baseline.get("artifact"), "manifest baseline artifact"
        )
        if manifest_baseline.get("generation_id") != report_baseline.get(
            "generation_id"
        ) or manifest_baseline_artifact.get("sha256") != report_baseline.get(
            "catalog_sha256"
        ):
            raise ControlPanelValidationError(
                "financial manifest baseline differs from the report authority"
            )
    legacy, source, legacy_snapshots = _source_inputs_snapshot(legacy_panel)
    template_files: dict[str, bytes] = {}
    template_raw_hashes: dict[str, str] = {}
    template_hashes: dict[str, str] = {}
    for template_name in ("index.html", "analytics.css", "app.js"):
        normalized, raw_sha256 = _template_snapshot(template_root / template_name)
        template_files[template_name] = normalized
        template_raw_hashes[template_name] = raw_sha256
        template_hashes[template_name] = hashlib.sha256(normalized).hexdigest()
    strategy_evidence_content: bytes | None = None
    strategy_evidence_sha256: str | None = None
    if strategy_evidence_path is not None:
        strategy_evidence_content, strategy_evidence_sha256 = _read_snapshot(
            strategy_evidence_path, "strategy evidence"
        )
    authority_snapshots: dict[Path, tuple[bytes, str]] = {
        FINANCIAL_MVP_CONTRACT: financial_contract_snapshot
    }
    for authority_path, authority_label in (
        (HWM_POLICY, "HWM policy"),
        (_PORTABLE_HWM_POLICY, "portable HWM policy"),
        (_PORTABLE_HWM_PERFORMANCE_POLICY, "portable HWM performance policy"),
        (ECONOMIC_DRIVER_CLUSTERS, "economic driver cluster contract"),
        (STRATEGY_UNDERWRITING_POLICY, "underwriting policy"),
        (PANEL_DATA_SCHEMA, "control-panel data schema"),
        (ACCOUNT_MANIFEST_SCHEMA, "account-strategy manifest schema"),
    ):
        authority_snapshots[authority_path] = _read_snapshot(
            authority_path, authority_label
        )
    data = _panel_data(
        report,
        report_identity_values,
        decomposition,
        decomposition_identity,
        correlation,
        correlation_identity,
        legacy,
        source,
        template_root,
        template_hashes,
        strategy_evidence_path,
        strategy_evidence_content,
        lens_payload,
        weekly_payload,
        freshness_health,
        replication_payload,
        candidate_driver_map,
        candidate_driver_map_identity,
        baseline_catalog,
        hwm_portable_receipt,
        hwm_portable_generation_bytes,
        hwm_portable_calendar_bytes,
        hwm_governed_result,
        hwm_governed_generation_bytes,
        hwm_governed_calendar_bytes,
        hwm_governed_receipt_bytes,
        account_manifest=account_manifest,
        account_manifest_identity=account_manifest_identity,
        financial_contract_snapshot=authority_snapshots[FINANCIAL_MVP_CONTRACT][0],
        financial_input_contract_snapshot=financial_input_contract_bytes,
        hwm_policy_snapshot=authority_snapshots[HWM_POLICY][0],
        portable_hwm_policy_snapshot=authority_snapshots[_PORTABLE_HWM_POLICY][0],
        portable_hwm_performance_policy_snapshot=authority_snapshots[
            _PORTABLE_HWM_PERFORMANCE_POLICY
        ][0],
        economic_driver_snapshot=authority_snapshots[ECONOMIC_DRIVER_CLUSTERS][0],
        underwriting_policy_snapshot=authority_snapshots[STRATEGY_UNDERWRITING_POLICY][
            0
        ],
        correlation_strategy_names=plan_strategy_names or baseline_strategy_names,
        epoch_hpr_receipts=(
            verified_strategy_history["epoch_hpr_receipts"]
            if verified_strategy_history is not None
            else None
        ),
    )
    if invested_capital is not None:
        data["invested_capital"] = invested_capital
    # PANEL-VIS-002: the report's portfolio-level totals ride along unchanged.
    for totals_key in PORTFOLIO_TOTALS_KEYS:
        if totals_key in report:
            data[totals_key] = report[totals_key]
    # The Performance tab reads the totals above (summed-epoch TWR and difference),
    # so it is built after they are attached.
    data["performance_tab"] = build_performance_tab(data)
    wire_data = _panel_wire_form(data)
    _validate_panel_data_schema(
        wire_data,
        authority_snapshots[PANEL_DATA_SCHEMA][0],
        authority_snapshots[ACCOUNT_MANIFEST_SCHEMA][0],
    )
    data_bytes = canonical_json_bytes(wire_data)
    legacy_data_path, _legacy_data_content, legacy_data_sha256 = legacy_snapshots[
        "legacy_data"
    ]
    legacy_styles_path, legacy_styles_content, legacy_styles_sha256 = (
        _frontend_styles_source(template_root, legacy_snapshots["source:styles.css"])
    )
    files = {
        "index.html": template_files["index.html"],
        "styles.css": legacy_styles_content,
        "analytics.css": template_files["analytics.css"],
        "app.js": template_files["app.js"],
        "data/dashboard_data.json": data_bytes,
        "data/dashboard_data.js": _panel_data_script(wire_data, data_bytes),
        "data/freshness-health.json": freshness_content,
    }
    if candidate_driver_map_content is not None:
        files["data/candidate-driver-map.json"] = candidate_driver_map_content
    downloads, download_bindings = _report_downloads(
        report_path,
        report_identity,
        report_root,
        require_workbook=False,
    )
    if files["index.html"].count(_DOWNLOAD_PLACEHOLDER) != 1:
        raise ControlPanelValidationError("report download template location differs")
    links = [
        _report_download_link(relative, label, downloads[relative])
        for relative, label in (
            (REPORT_DOWNLOAD, "Download report data"),
            (WORKBOOK_DOWNLOAD, "Download Excel workbook"),
        )
        if relative in downloads
    ]
    files["index.html"] = files["index.html"].replace(
        _DOWNLOAD_PLACEHOLDER, " ".join(links).encode("utf-8")
    )
    if len(files["index.html"]) > _INDEX_HTML_MAX_BYTES:
        raise ControlPanelValidationError(
            "index.html exceeds the panel size budget: "
            f"{len(files['index.html'])} bytes > {_INDEX_HTML_MAX_BYTES}"
        )
    # Neither download is written beside the page. Both are served from the
    # base64 payload inlined above: `app.js` intercepts the click and rebuilds the
    # file from those chunks, and only reads the href as an identifier.
    #
    # The workbook: an .xlsx in the generation directory is pure liability -- the
    # production roots live under a SharePoint sync root, and the sync client
    # rewrites Office documents in place, injecting `customXml` document
    # properties. That silently changes bytes inside a published,
    # content-addressed generation: it is what left the 2026-08-16 report pointer
    # unverifiable, and what made two byte-identical copies of this workbook
    # diverge from each other during materialisation.
    #
    # The report JSON (#540): the page already carries it byte for byte, the
    # report generation it came from stays hash-bound by `meta.report_sha256` and
    # the `report` input binding, and `candidate.json` records the embedded
    # digest in `embedded_artifacts`. A second 330 MB copy under `downloads/`
    # added no proof and doubled the generation. Readers take it from the page
    # through `embedded_download_bytes`.
    bound_inputs: dict[str, tuple[Path, str]] = {
        "report": (report_path, report_identity_values["sha256"]),
        "decomposition": (decomposition_path, decomposition_identity["sha256"]),
        "correlation": (correlation_path, correlation_identity["sha256"]),
        "legacy": (legacy_data_path, legacy_data_sha256),
        "legacy_styles": (legacy_styles_path, legacy_styles_sha256),
        "freshness": (freshness_health_path, freshness_sha256),
        "financial_mvp_contract": (
            FINANCIAL_MVP_CONTRACT,
            authority_snapshots[FINANCIAL_MVP_CONTRACT][1],
        ),
        "hwm_policy": (HWM_POLICY, authority_snapshots[HWM_POLICY][1]),
        "portable_hwm_policy": (
            _PORTABLE_HWM_POLICY,
            authority_snapshots[_PORTABLE_HWM_POLICY][1],
        ),
        "portable_hwm_performance_policy": (
            _PORTABLE_HWM_PERFORMANCE_POLICY,
            authority_snapshots[_PORTABLE_HWM_PERFORMANCE_POLICY][1],
        ),
        "economic_driver_clusters": (
            ECONOMIC_DRIVER_CLUSTERS,
            authority_snapshots[ECONOMIC_DRIVER_CLUSTERS][1],
        ),
        "underwriting_policy": (
            STRATEGY_UNDERWRITING_POLICY,
            authority_snapshots[STRATEGY_UNDERWRITING_POLICY][1],
        ),
        "panel_data_schema": (
            PANEL_DATA_SCHEMA,
            authority_snapshots[PANEL_DATA_SCHEMA][1],
        ),
        "account_manifest_schema": (
            ACCOUNT_MANIFEST_SCHEMA,
            authority_snapshots[ACCOUNT_MANIFEST_SCHEMA][1],
        ),
    }
    bound_inputs.update(download_bindings)
    if strategy_history_binding is not None:
        bound_inputs["strategy_history_contract"] = strategy_history_binding
    if invested_capital_input_generation_path is not None:
        bound_inputs["financial_input_contract"] = (
            financial_input_contract_path,
            hashlib.sha256(financial_input_contract_bytes).hexdigest(),
        )
        capital_input, capital_input_sha256 = _read_snapshot(
            invested_capital_input_generation_path,
            "invested-capital input generation",
        )
        bound_inputs["invested_capital_input_generation"] = (
            invested_capital_input_generation_path,
            capital_input_sha256,
        )
    if (
        account_manifest_identity is not None
        and account_manifest_identity.get("authority_profile")
        == MANIFEST_AUTHORITY_VERIFIED_BUNDLE
    ):
        assert account_manifest_generation_root is not None
        assert account_manifest_generation_id is not None
        bound_inputs["account_manifest"] = (
            account_manifest_generation_root
            / "generations"
            / account_manifest_generation_id
            / _EPOCH_MANIFEST_PATH,
            _text(
                account_manifest_identity.get("sha256"),
                "account manifest sha256",
            ),
        )
    for source_name, (source_path, _content, source_sha256) in legacy_snapshots.items():
        bound_inputs.setdefault(
            f"legacy_source:{source_name}", (source_path, source_sha256)
        )

    def bind_current_generation(
        prefix: str, identity: Mapping[str, str | None]
    ) -> None:
        manifest_path = identity.get("manifest_path")
        manifest_sha256 = identity.get("manifest_sha256")
        pointer_path = identity.get("pointer_path")
        pointer_sha256 = identity.get("pointer_sha256")
        if (
            manifest_path is None
            or manifest_sha256 is None
            or pointer_path is None
            or pointer_sha256 is None
        ):
            return
        bound_inputs[f"{prefix}:manifest"] = (
            Path(manifest_path),
            manifest_sha256,
        )
        bound_inputs[f"{prefix}:pointer"] = (
            Path(pointer_path),
            pointer_sha256,
        )

    bind_current_generation("report", report_identity)
    bind_current_generation("decomposition", decomposition_identity)
    bind_current_generation("correlation", correlation_identity)
    for template_name in ("index.html", "analytics.css", "app.js"):
        bound_inputs[f"template:{template_name}"] = (
            template_root / template_name,
            template_raw_hashes[template_name],
        )
    for baseline_name, binding in baseline_bindings.items():
        bound_inputs[f"baseline:{baseline_name}"] = binding
    if financial_mode:
        assert account_manifest is not None
        assert account_manifest_identity is not None
        assert account_manifest_path is not None
        assert manifest_ledger_path is not None
        assert manifest_ledger_identity_path is not None
        assert manifest_baseline_root is not None
        assert manifest_classified_flows_path is not None
        assert manifest_flow_coverage_path is not None
        assert manifest_classification_policy_path is not None
        assert manifest_admission_policy_path is not None
        manifest_sources = _object(
            account_manifest.get("source_identities"),
            "account manifest source identities",
        )

        def source_artifact_sha256(label: str) -> str:
            source = _object(manifest_sources.get(label), f"manifest {label}")
            artifact = _object(source.get("artifact"), f"manifest {label} artifact")
            return _text(artifact.get("sha256"), f"manifest {label} sha256")

        ledger_source = _object(manifest_sources.get("ledger"), "manifest ledger")
        ledger_identity_artifact = _object(
            ledger_source.get("identity_artifact"),
            "manifest ledger identity artifact",
        )
        calculation_code = _object(
            manifest_sources.get("calculation_code"),
            "manifest calculation code",
        )
        bound_inputs.update(
            {
                "account_manifest": (
                    account_manifest_path,
                    _text(
                        account_manifest_identity.get("sha256"),
                        "account manifest sha256",
                    ),
                ),
                "manifest_ledger": (
                    manifest_ledger_path,
                    source_artifact_sha256("ledger"),
                ),
                "manifest_ledger_identity": (
                    manifest_ledger_identity_path,
                    _text(
                        ledger_identity_artifact.get("sha256"),
                        "manifest ledger identity sha256",
                    ),
                ),
                "manifest_classified_flows": (
                    manifest_classified_flows_path,
                    source_artifact_sha256("classified_flows"),
                ),
                "manifest_flow_coverage": (
                    manifest_flow_coverage_path,
                    source_artifact_sha256("flow_coverage"),
                ),
                "manifest_classification_policy": (
                    manifest_classification_policy_path,
                    source_artifact_sha256("flow_classification_policy"),
                ),
                "manifest_admission_policy": (
                    manifest_admission_policy_path,
                    source_artifact_sha256("admission_policy"),
                ),
                "manifest_baseline_catalog": (
                    manifest_baseline_root
                    / "generations"
                    / _text(
                        _object(
                            manifest_sources.get("baseline"), "manifest baseline"
                        ).get("generation_id"),
                        "manifest baseline generation_id",
                    )
                    / "portfolio-baseline.json",
                    source_artifact_sha256("baseline"),
                ),
                "manifest_calculation_code": (
                    ACCOUNT_MANIFEST_CALCULATION_CODE,
                    _text(
                        calculation_code.get("sha256"),
                        "manifest calculation code sha256",
                    ),
                ),
            }
        )
    optional_bindings = {
        "strategy_evidence": (
            strategy_evidence_path,
            strategy_evidence_sha256,
        ),
        "weekly_returns": (weekly_path, data["meta"].get("weekly_returns_sha256")),
        "replication": (replication_path, data["meta"].get("replication_sha256")),
        "candidate_driver_map": (
            candidate_driver_map_path,
            (
                candidate_driver_map_identity.get("sha256")
                if candidate_driver_map_identity is not None
                else None
            ),
        ),
    }
    for name, (path, expected_sha256) in optional_bindings.items():
        if path is not None and expected_sha256 is not None:
            bound_inputs[name] = (path, str(expected_sha256))
    if weekly_path is not None:
        bind_current_generation("weekly_returns", weekly_identity)
    if replication_path is not None:
        bind_current_generation("replication", replication_identity)
    for index, (path, sha256) in enumerate(lens_bindings):
        bound_inputs[f"decomposition_lens:{index:02d}"] = (path, sha256)
    return files, data, bound_inputs


def _panel_id(files: Mapping[str, bytes]) -> str:
    identity = {
        name: hashlib.sha256(content).hexdigest()
        for name, content in sorted(files.items())
    }
    return hashlib.sha256(canonical_json_bytes(identity)).hexdigest()


def _embedded_artifact_records(files: Mapping[str, bytes]) -> dict[str, Any]:
    """Digest and size of each download the page carries instead of a file."""

    html = files["index.html"].decode("utf-8")
    records: dict[str, Any] = {}
    for relative, (_name, _mime, prefix) in sorted(_DOWNLOAD_METADATA.items()):
        if relative in files or f'data-download-payload-prefix="{prefix}"' not in html:
            continue
        content = embedded_download_bytes(html, relative)
        records[relative] = {
            "encoding": _DOWNLOAD_ENCODINGS[relative],
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        }
    return records


def _candidate_record(
    files: Mapping[str, bytes], data: Mapping[str, Any]
) -> dict[str, Any]:
    panel_id = _panel_id(files)
    return {
        "account_manifest_authority_profile": data["meta"][
            "account_manifest_authority_profile"
        ],
        "account_manifest_verification": data["meta"]["account_manifest_verification"],
        "artifact_sha256": {
            name: hashlib.sha256(content).hexdigest()
            for name, content in sorted(files.items())
        },
        "panel_id": panel_id,
        "panel_schema_version": data["panel_schema_version"],
        "portfolio_as_of": data["meta"]["portfolio_as_of"],
        "correlation_generation_id": data["meta"]["correlation_generation_id"],
        "decomposition_generation_id": data["meta"]["decomposition_generation_id"],
        "embedded_artifacts": _embedded_artifact_records(files),
        "report_generation_id": data["meta"]["report_generation_id"],
        "state": "STAGED",
    }


def _write_exact(path: Path, content: bytes) -> None:
    candidate = _filesystem_path(path)
    candidate.parent.mkdir(parents=True, exist_ok=True)
    if candidate.exists():
        if candidate.is_symlink() or candidate.read_bytes() != content:
            raise GenerationConflictError(f"candidate file differs: {path}")
        return
    descriptor = os.open(
        candidate,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def build_candidate(
    report_root: Path | None,
    legacy_panel: Path,
    output_root: Path,
    template_root: Path = DEFAULT_TEMPLATE_ROOT,
    decomposition_root: Path | None = None,
    correlation_root: Path | None = None,
    strategy_evidence_path: Path | None = None,
    decomposition_lenses_root: Path | None = None,
    decomposition_plan_bundle: Path | None = None,
    weekly_returns_root: Path | None = None,
    freshness_health_path: Path | None = None,
    replication_root: Path | None = None,
    candidate_driver_map_path: Path | None = None,
    baseline_root: Path | None = None,
    hwm_portable_receipt: Mapping[str, Any] | None = None,
    hwm_portable_generation_bytes: bytes | None = None,
    hwm_portable_calendar_bytes: bytes | None = None,
    hwm_governed_result: Mapping[str, Any] | None = None,
    hwm_governed_generation_bytes: bytes | None = None,
    hwm_governed_calendar_bytes: bytes | None = None,
    hwm_governed_receipt_bytes: bytes | None = None,
    account_manifest_path: Path | None = None,
    financial_mode: bool = False,
    manifest_ledger_path: Path | None = None,
    manifest_ledger_identity_path: Path | None = None,
    manifest_baseline_root: Path | None = None,
    manifest_classified_flows_path: Path | None = None,
    manifest_flow_coverage_path: Path | None = None,
    manifest_classification_policy_path: Path | None = None,
    manifest_admission_policy_path: Path | None = None,
    *,
    invested_capital_result: Mapping[str, Any] | None = None,
    invested_capital_input_generation_path: Path | None = None,
    report_candidate: Path | None = None,
    strategy_history_contract_path: Path | None = None,
    account_manifest_generation_root: Path | None = None,
    account_manifest_generation_id: str | None = None,
    manifest_membership_census_path: Path | None = None,
    hybrid_decomposition_plan_bundle: Path | None = None,
    dependence_selected_inputs_path: Path | None = None,
) -> dict[str, Any]:
    files, data, _ = _rendered_files(
        report_root,
        legacy_panel,
        template_root,
        decomposition_root,
        correlation_root,
        strategy_evidence_path,
        decomposition_lenses_root,
        decomposition_plan_bundle,
        weekly_returns_root,
        freshness_health_path,
        replication_root,
        candidate_driver_map_path,
        baseline_root,
        hwm_portable_receipt,
        hwm_portable_generation_bytes,
        hwm_portable_calendar_bytes,
        hwm_governed_result,
        hwm_governed_generation_bytes,
        hwm_governed_calendar_bytes,
        hwm_governed_receipt_bytes,
        account_manifest_path=account_manifest_path,
        financial_mode=financial_mode,
        manifest_ledger_path=manifest_ledger_path,
        manifest_ledger_identity_path=manifest_ledger_identity_path,
        manifest_baseline_root=manifest_baseline_root,
        manifest_classified_flows_path=manifest_classified_flows_path,
        manifest_flow_coverage_path=manifest_flow_coverage_path,
        manifest_classification_policy_path=manifest_classification_policy_path,
        manifest_admission_policy_path=manifest_admission_policy_path,
        report_candidate=report_candidate,
        strategy_history_contract_path=strategy_history_contract_path,
        account_manifest_generation_root=account_manifest_generation_root,
        account_manifest_generation_id=account_manifest_generation_id,
        manifest_membership_census_path=manifest_membership_census_path,
        invested_capital_result=invested_capital_result,
        invested_capital_input_generation_path=(invested_capital_input_generation_path),
        hybrid_decomposition_plan_bundle=hybrid_decomposition_plan_bundle,
        dependence_selected_inputs_path=dependence_selected_inputs_path,
    )
    panel_id = _panel_id(files)
    candidate = output_root / "candidates" / panel_id
    for relative, content in files.items():
        _write_exact(candidate / relative, content)
    manifest = _candidate_record(files, data)
    _write_exact(candidate / "candidate.json", canonical_json_bytes(manifest))
    return {
        "account_manifest_authority_profile": data["meta"][
            "account_manifest_authority_profile"
        ],
        "candidate_dir": str(candidate),
        "candidate_rows": len(data["candidate_appendix"]["providers"]),
        "correlation_pair_count": data["dependence_analysis"]["pair_count"],
        "factor_model_count": data["factor_analysis"]["available_count"],
        "panel_id": panel_id,
        "strategy_count": data["counts"]["strategies"],
        "twr_available_count": data["counts"]["twr_available"],
    }


def _check(name: str, condition: bool, evidence: object) -> dict[str, Any]:
    return {
        "check": name,
        "evidence": evidence,
        "status": "PASS" if condition else "FAIL",
    }


def _replication_disposition_counts(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, int] | None:
    statuses = Counter(str(row.get("availability_status")) for row in rows)
    supported = {
        "AVAILABLE_RESEARCH_REPLICATION",
        "DIAGNOSTIC_ONLY_SOURCE_MODEL",
        "UNAVAILABLE_NO_SELECTED_DYNAMIC_MODEL",
        "UNAVAILABLE_NOT_CONFIGURED",
    }
    if set(statuses) - supported:
        return None
    return {
        "available_count": statuses["AVAILABLE_RESEARCH_REPLICATION"],
        "diagnostic_count": statuses["DIAGNOSTIC_ONLY_SOURCE_MODEL"],
        "unavailable_count": (
            statuses["UNAVAILABLE_NO_SELECTED_DYNAMIC_MODEL"]
            + statuses["UNAVAILABLE_NOT_CONFIGURED"]
        ),
    }


def _check_implementation_layers(
    underwriting_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Recompute the underwriting gap and implementation layers from their rows.

    A stale kind is a closed NOT_READY object: its shape is validated, it carries no
    bridge to reconcile, and the layer must report it as not ready.  The strategies
    whose implementation layer is accounted for are the causal, the policy-unavailable
    and the stale-attribution ones.
    """

    gap_reconciles = True
    causal_attribution_reconciles = True
    causal_attribution_strategy_ids: set[str] = set()
    policy_unavailable_strategy_ids: set[str] = set()
    stale_attribution_strategy_ids: set[str] = set()
    stale_reconciliation_strategy_ids: set[str] = set()
    stale_shapes_valid = True
    for row in underwriting_rows:
        gap = _object(row.get("model_account_gap"), "underwriting model-account gap")
        total_gap = _optional_float(gap.get("total_gap"), "underwriting total gap")
        residual = _optional_float(
            gap.get("unresolved_residual"), "underwriting unresolved residual"
        )
        reconciliation_error = _optional_float(
            gap.get("reconciliation_error"), "underwriting reconciliation error"
        )
        components = _objects(
            gap.get("causally_bound_components"),
            "underwriting causally bound components",
        )
        if components or (
            total_gap is not None
            and (
                residual is None
                or reconciliation_error is None
                or abs(total_gap - residual) > 1e-12
                or abs(reconciliation_error) > 1e-12
            )
        ):
            gap_reconciles = False
            break
        layers = _object(row.get("evidence_layers"), "underwriting evidence layers")
        implementation = _object(
            layers.get("implementation_reconciliation"),
            "underwriting implementation reconciliation",
        )
        detail_value = implementation.get("detail")
        stale_strategy_id = _text(row.get("strategy_id"), "underwriting strategy_id")
        comparison_status = implementation.get("comparison_status")
        if detail_value is None:
            if comparison_status == STALE_IMPLEMENTATION_STATUS:
                stale_shapes_valid = False
            continue
        detail = _object(detail_value, "underwriting implementation detail")
        stale_present = False
        for stale_key, kind, stale_ids, live_key in (
            (
                "stale_reconciliation",
                "RECONCILIATION",
                stale_reconciliation_strategy_ids,
                "return_reconciliation",
            ),
            (
                "stale_return_attribution",
                "RETURN_ATTRIBUTION",
                stale_attribution_strategy_ids,
                "return_attribution",
            ),
        ):
            if stale_key not in detail:
                continue
            stale_present = True
            stale_ids.add(stale_strategy_id)
            try:
                _validate_stale_implementation_object(
                    detail[stale_key], kind=kind, strategy_id=stale_strategy_id
                )
            except ControlPanelValidationError:
                stale_shapes_valid = False
            if live_key in detail:
                stale_shapes_valid = False
        if stale_present != (comparison_status == STALE_IMPLEMENTATION_STATUS):
            stale_shapes_valid = False
        attribution_value = detail.get("return_attribution")
        if attribution_value is None:
            continue
        attribution = _object(
            attribution_value, "underwriting implementation return attribution"
        )
        bridge = _object(attribution.get("bridge"), "underwriting causal return bridge")
        if bridge.get("status") == "UNAVAILABLE_POLICY_NOT_ADMITTED":
            import portfolio_report as reporter

            strategy_id = _text(row.get("strategy_id"), "underwriting strategy_id")
            metrics = _object(
                detail.get("return_reconciliation"), "underwriting return comparison"
            )
            reporter._validate_policy_unavailable_comparison(
                metrics, strategy_id=strategy_id
            )
            reporter._validate_policy_unavailable_comparison(
                bridge,
                strategy_id=strategy_id,
                attribution=True,
                expected_policy_evidence=_object(
                    metrics.get("policy_evidence"), "underwriting policy evidence"
                ),
            )
            if gap.get("status") != "UNAVAILABLE_POLICY_NOT_ADMITTED" or any(
                gap.get(key) is not None
                for key in (
                    "total_gap",
                    "reference_cumulative_return",
                    "beta",
                    "correlation",
                    "tracking_error_annualized",
                )
            ):
                causal_attribution_reconciles = False
                break
            policy_unavailable_strategy_ids.add(strategy_id)
            continue
        model_return = _finite_float(
            bridge.get("model_return"), "underwriting causal model return"
        )
        account_return = _finite_float(
            bridge.get("account_return"), "underwriting causal account return"
        )
        gap_pp = _finite_float(bridge.get("gap_pp"), "underwriting causal gap")
        category_total = sum(
            _finite_float(
                category.get("effect_pp"), "underwriting causal category effect"
            )
            for category in _objects(
                bridge.get("categories"), "underwriting causal categories"
            )
        )
        strategy_id = _text(row.get("strategy_id"), "underwriting strategy_id")
        causal_attribution_strategy_ids.add(strategy_id)
        if not math.isclose(
            gap_pp, (account_return - model_return) * 100.0, abs_tol=1e-10
        ) or not math.isclose(gap_pp, category_total, abs_tol=1e-8):
            causal_attribution_reconciles = False
            break
    return {
        "gap_reconciles": gap_reconciles,
        "causal_attribution_reconciles": causal_attribution_reconciles,
        "causal_attribution_strategy_ids": sorted(causal_attribution_strategy_ids),
        "policy_unavailable_strategy_ids": sorted(policy_unavailable_strategy_ids),
        "stale_attribution_strategy_ids": sorted(stale_attribution_strategy_ids),
        "stale_reconciliation_strategy_ids": sorted(stale_reconciliation_strategy_ids),
        "stale_shapes_valid": stale_shapes_valid,
        "implementation_strategy_ids": sorted(
            causal_attribution_strategy_ids
            | policy_unavailable_strategy_ids
            | stale_attribution_strategy_ids
        ),
    }


def verify_candidate(
    report_root: Path | None,
    legacy_panel: Path,
    candidate_dir: Path,
    template_root: Path = DEFAULT_TEMPLATE_ROOT,
    decomposition_root: Path | None = None,
    correlation_root: Path | None = None,
    strategy_evidence_path: Path | None = None,
    decomposition_lenses_root: Path | None = None,
    decomposition_plan_bundle: Path | None = None,
    weekly_returns_root: Path | None = None,
    freshness_health_path: Path | None = None,
    replication_root: Path | None = None,
    candidate_driver_map_path: Path | None = None,
    baseline_root: Path | None = None,
    hwm_portable_receipt: Mapping[str, Any] | None = None,
    hwm_portable_generation_bytes: bytes | None = None,
    hwm_portable_calendar_bytes: bytes | None = None,
    hwm_governed_result: Mapping[str, Any] | None = None,
    hwm_governed_generation_bytes: bytes | None = None,
    hwm_governed_calendar_bytes: bytes | None = None,
    hwm_governed_receipt_bytes: bytes | None = None,
    account_manifest_path: Path | None = None,
    financial_mode: bool = False,
    manifest_ledger_path: Path | None = None,
    manifest_ledger_identity_path: Path | None = None,
    manifest_baseline_root: Path | None = None,
    manifest_classified_flows_path: Path | None = None,
    manifest_flow_coverage_path: Path | None = None,
    manifest_classification_policy_path: Path | None = None,
    manifest_admission_policy_path: Path | None = None,
    *,
    invested_capital_result: Mapping[str, Any] | None = None,
    invested_capital_input_generation_path: Path | None = None,
    report_candidate: Path | None = None,
    strategy_history_contract_path: Path | None = None,
    account_manifest_generation_root: Path | None = None,
    account_manifest_generation_id: str | None = None,
    manifest_membership_census_path: Path | None = None,
    hybrid_decomposition_plan_bundle: Path | None = None,
    dependence_selected_inputs_path: Path | None = None,
    _rendered_bundle: list[
        tuple[dict[str, bytes], dict[str, Any], dict[str, tuple[Path, str]]]
    ]
    | None = None,
) -> dict[str, Any]:
    expected, data, _ = _rendered_files(
        report_root,
        legacy_panel,
        template_root,
        decomposition_root,
        correlation_root,
        strategy_evidence_path,
        decomposition_lenses_root,
        decomposition_plan_bundle,
        weekly_returns_root,
        freshness_health_path,
        replication_root,
        candidate_driver_map_path,
        baseline_root,
        hwm_portable_receipt,
        hwm_portable_generation_bytes,
        hwm_portable_calendar_bytes,
        hwm_governed_result,
        hwm_governed_generation_bytes,
        hwm_governed_calendar_bytes,
        hwm_governed_receipt_bytes,
        account_manifest_path=account_manifest_path,
        financial_mode=financial_mode,
        manifest_ledger_path=manifest_ledger_path,
        manifest_ledger_identity_path=manifest_ledger_identity_path,
        manifest_baseline_root=manifest_baseline_root,
        manifest_classified_flows_path=manifest_classified_flows_path,
        manifest_flow_coverage_path=manifest_flow_coverage_path,
        manifest_classification_policy_path=manifest_classification_policy_path,
        manifest_admission_policy_path=manifest_admission_policy_path,
        report_candidate=report_candidate,
        strategy_history_contract_path=strategy_history_contract_path,
        account_manifest_generation_root=account_manifest_generation_root,
        account_manifest_generation_id=account_manifest_generation_id,
        manifest_membership_census_path=manifest_membership_census_path,
        invested_capital_result=invested_capital_result,
        invested_capital_input_generation_path=(invested_capital_input_generation_path),
        hybrid_decomposition_plan_bundle=hybrid_decomposition_plan_bundle,
        dependence_selected_inputs_path=dependence_selected_inputs_path,
    )
    if _rendered_bundle is not None:
        _rendered_bundle.append((expected, data, _))
    panel_id = _panel_id(expected)
    checks: list[dict[str, Any]] = []
    checks.append(
        _check("Candidate identity", candidate_dir.name == panel_id, panel_id)
    )
    expected_candidate_record = _candidate_record(expected, data)
    try:
        candidate_record = _load_json(
            candidate_dir / "candidate.json", "candidate record"
        )
    except ControlPanelValidationError:
        candidate_record = {}
    checks.append(
        _check(
            "Candidate record binds exact files and authority profile",
            candidate_record == expected_candidate_record,
            {
                "actual": candidate_record,
                "expected": expected_candidate_record,
            },
        )
    )
    exact = True
    for relative, content in expected.items():
        path = candidate_dir / relative
        try:
            observed, _sha256 = _read_snapshot(path, f"candidate {relative}")
        except ControlPanelValidationError:
            matches = False
        else:
            matches = observed == content
        exact = exact and matches
    checks.append(_check("Candidate files are deterministic", exact, sorted(expected)))
    resources_closed, resource_evidence = _closed_frontend_resources(candidate_dir)
    checks.append(
        _check(
            "Closed local frontend resource allowlist",
            resources_closed,
            resource_evidence,
        )
    )
    html = expected["index.html"].decode("utf-8")
    javascript = expected["app.js"].decode("utf-8")
    checks.append(
        _check(
            "All nine operator views are present",
            all(f'data-view="{view}"' in html for view in VIEWS),
            list(VIEWS),
        )
    )
    lens_payload = _object(data.get("decomposition_lenses"), "decomposition lenses")
    lens_rows = _objects(lens_payload.get("lenses"), "decomposition lenses")
    required_analysis_series = {
        _text(row.get("strategy_name"), "strategy.strategy_name")
        for row in _current_strategy_rows(data)
        if row.get("product_requirement") == "REQUIRED"
    }
    if not required_analysis_series:
        required_analysis_series = {
            _text(item, "dependence series_id")
            for item in data["dependence_analysis"].get("series_ids", [])
        }
    expected_lens_ids = (
        {"DYNAMIC_STYLE"}
        if decomposition_lenses_root is None
        else {
            "DYNAMIC_STYLE",
            "ECONOMIC_FACTOR",
            "ETF_STYLE",
            "RETURN_INDUSTRY",
            "RETURN_SECTOR",
        }
    )
    checks.append(
        _check(
            "Decomposition lenses remain separate and complete",
            {_text(row.get("lens_id"), "lens_id") for row in lens_rows}
            == expected_lens_ids
            and all(
                {
                    _text(item.get("series_id"), "lens series_id")
                    for item in _objects(row.get("series"), "lens series")
                }
                == required_analysis_series
                for row in lens_rows
            ),
            {
                "lenses": sorted(
                    _text(row.get("lens_id"), "lens_id") for row in lens_rows
                ),
                "plan_bundle_id": lens_payload.get("plan_bundle_id"),
            },
        )
    )
    replication_analysis = _object(
        data.get("replication_analysis"), "replication analysis"
    )
    replication_rows = _objects(
        replication_analysis.get("series"), "replication analysis series"
    )
    current_strategy_ids = {
        _text(row.get("strategy_id"), "current strategy_id")
        for row in _current_strategy_rows(data)
    }
    replication_counts = _replication_disposition_counts(replication_rows)
    counts_match_rows = replication_counts is not None and all(
        replication_analysis.get(field) == count
        for field, count in replication_counts.items()
    )
    checks.append(
        _check(
            "Dynamic replication is governed or explicitly unavailable",
            len(replication_rows) == len(current_strategy_ids)
            and (
                (
                    replication_root is not None
                    and replication_analysis.get("generation_id") is not None
                    and counts_match_rows
                    and all(
                        row.get("availability_status") != "UNAVAILABLE_NOT_CONFIGURED"
                        for row in replication_rows
                    )
                )
                or (
                    replication_root is None
                    and replication_analysis.get("generation_id") is None
                    and counts_match_rows
                    and all(
                        row.get("availability_status") == "UNAVAILABLE_NOT_CONFIGURED"
                        for row in replication_rows
                    )
                )
            ),
            {
                "available": replication_analysis.get("available_count"),
                "diagnostic": replication_analysis.get("diagnostic_count"),
                "generation": replication_analysis.get("generation_id"),
                "unavailable": replication_analysis.get("unavailable_count"),
            },
        )
    )
    performance_tab = data.get("performance_tab")
    performance_tab_error: str | None = None
    try:
        if not isinstance(performance_tab, Mapping):
            raise ValueError("performance_tab is absent")
        verify_performance_tab(performance_tab, data)
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        performance_tab_error = str(exc)
    checks.append(
        _check(
            "Performance tab re-derives from the panel inputs",
            performance_tab_error is None,
            performance_tab_error,
        )
    )
    weekly_payload = _object(data.get("weekly_returns"), "weekly returns")
    weekly_series = _objects(weekly_payload.get("series"), "weekly series")
    weekly_expected = weekly_returns_root is not None
    expected_weekly_strategy_count = sum(
        row.get("product_requirement") != "EXCLUDED"
        for row in _current_strategy_rows(data)
    )
    checks.append(
        _check(
            "Weekly return monitor is governed or explicitly unavailable",
            (
                weekly_expected
                and weekly_payload.get("generation_id") is not None
                and sum(row.get("primary_group") == "STRATEGY" for row in weekly_series)
                == expected_weekly_strategy_count
                and sum(
                    "CORE_BENCHMARK" in row.get("display_groups", [])
                    for row in weekly_series
                )
                == CORE_BENCHMARK_COUNT
                and weekly_payload.get("default_evidence_mode") == "LIVE_ONLY"
                and _object(
                    weekly_payload.get("current_week"), "weekly current_week"
                ).get("default_visible")
                is True
                and _object(
                    weekly_payload.get("current_week"), "weekly current_week"
                ).get("data_through_date")
                == weekly_payload.get("as_of_date")
            )
            or (
                not weekly_expected
                and weekly_payload.get("status") == "UNAVAILABLE_NOT_CONFIGURED"
            ),
            {
                "generation": weekly_payload.get("generation_id"),
                "series": len(weekly_series),
                "status": weekly_payload.get("status", "AVAILABLE"),
            },
        )
    )
    canonical_series = _objects(
        data.get("canonical_strategy_series", []), "canonical strategy series"
    )
    expected_canonical_count = len(required_analysis_series)
    canonical_series_ids = {
        _text(row.get("series_id"), "canonical series_id") for row in canonical_series
    }
    factor_series_ids = {
        _text(row.get("series_id"), "factor series_id")
        for row in _objects(data["factor_analysis"].get("series"), "factor series")
    }
    dependence_series_ids = {
        _text(item, "dependence series_id")
        for item in data["dependence_analysis"].get("series_ids", [])
    }
    weekly_strategy_series_ids = {
        _text(row.get("display_name"), "weekly strategy display_name")
        for row in weekly_series
        if row.get("primary_group") == "STRATEGY"
    }
    checks.append(
        _check(
            "Canonical strategy series agree across report, factor, dependence, and weekly views",
            (not weekly_expected and not canonical_series)
            or (
                weekly_expected
                and expected_canonical_count > 0
                and len(canonical_series) == expected_canonical_count
                and canonical_series_ids
                == factor_series_ids
                == dependence_series_ids
                == weekly_strategy_series_ids
                == required_analysis_series
                and len(
                    {
                        (
                            row.get("series_id"),
                            row.get("evidence_layer"),
                            row.get("generation_id"),
                            row.get("strategy_version"),
                        )
                        for row in canonical_series
                    }
                )
                == expected_canonical_count
            ),
            {
                "canonical_series": len(canonical_series),
                "report_series": expected_canonical_count,
            },
        )
    )
    checks.append(
        _check(
            "No external frontend dependency",
            re.search(r'<(?:script|link)[^>]+(?:src|href)="https?://', html, re.I)
            is None,
            "relative local assets only",
        )
    )
    execution_pattern = re.compile(
        r"\b(fetch\s*\(|XMLHttpRequest|WebSocket|placeOrder|submitOrder|sendBeacon|brokerApi)\b",
        re.I,
    )
    checks.append(
        _check(
            "No network or trade execution code",
            execution_pattern.search(javascript) is None,
            "static embedded renderer",
        )
    )
    if report_candidate is None:
        assert report_root is not None
        report, _, _ = _current_report(report_root)
    else:
        report, _, _ = _report_candidate(report_candidate)
    report_expectations = _report_panel_expectations(report)
    # The capital block is the report's current capital (value, status, date) and
    # holds the identity recomputed from the panel's own strategy rows.  The old
    # check compared the report's rows with themselves.
    capital_problems = _capital_identity_problems(data)
    checks.append(
        _check(
            "Accepted current capital",
            data["capital"] == report_expectations["capital"]
            and data["capital_status"] == report_expectations["capital_status"]
            and data["capital_as_of"] == report_expectations["capital_as_of"]
            and data.get("custody_current_capital")
            == report_expectations.get("custody_current_capital")
            and not capital_problems,
            {
                "actual": {
                    "capital": data["capital"],
                    "capital_as_of": data["capital_as_of"],
                    "capital_status": data["capital_status"],
                },
                "expected": {
                    "capital": report_expectations["capital"],
                    "capital_as_of": report_expectations["capital_as_of"],
                    "capital_status": report_expectations["capital_status"],
                },
                "identity_problems": capital_problems,
            },
        )
    )
    checks.append(
        _check(
            "Current strategy boundary",
            data["counts"]["strategies"] == report_expectations["counts"]["strategies"]
            and len(data["allocations"]) == report_expectations["counts"]["allocations"]
            and data["counts"]["accounts"] == report_expectations["counts"]["accounts"],
            {
                "actual": {
                    "accounts": data["counts"]["accounts"],
                    "allocations": len(data["allocations"]),
                    "strategies": data["counts"]["strategies"],
                },
                "expected": report_expectations["counts"],
            },
        )
    )
    account_rows = _objects(data.get("account_summary"), "account_summary")
    account_ids = [
        _text(row.get("account_id"), "account_summary.account_id")
        for row in account_rows
    ]
    account_reference_ids = {
        _text(row.get("account_id"), "account reference")
        for row in (
            *_objects(data.get("performance"), "performance"),
            *_objects(data.get("positions"), "positions"),
        )
    }
    manifest_rows = (
        _objects(data["account_manifest"].get("rows"), "account_manifest.rows")
        if isinstance(data.get("account_manifest"), Mapping)
        else []
    )
    unresolved_references = _unresolved_account_references(
        account_ids, account_reference_ids, manifest_rows
    )
    checks.append(
        _check(
            "Privacy-safe account dimension is complete",
            len(account_rows) == report_expectations["counts"]["accounts"]
            and len(set(account_ids)) == len(account_rows)
            and not unresolved_references
            and all(
                row.get("provider_id") or row.get("provider") for row in account_rows
            )
            and not any(
                field in row
                for row in account_rows
                for field in (
                    "account_number",
                    "external_account_id",
                    "masked_account_number",
                )
            ),
            {
                "accounts": len(account_rows),
                "referenced_accounts": len(account_reference_ids),
                "unresolved_references": unresolved_references,
            },
        )
    )
    report_position_rows = _objects(report.get("positions", []), "report.positions")
    strategy_names = {
        _text(row.get("strategy_id"), "strategy.strategy_id"): _text(
            row.get("strategy_name"), "strategy.strategy_name"
        )
        for row in _objects(report.get("strategy_summary"), "strategy_summary")
    }
    classified_report_positions, expected_position_snapshots = _classify_positions(
        report_position_rows, _position_cutoff_date(report)
    )
    expected_position_rows = []
    expected_identity_index = _holdings_identity_index(report)
    for row in classified_report_positions:
        symbol_status, symbol_reason, symbol, asset_category = (
            _position_symbol_status_and_reason(row, expected_identity_index)
        )
        expected_position_rows.append(
            {
                **row,
                "asset_category": asset_category,
                "symbol": symbol,
                "strategy_name": (
                    None
                    if row.get("strategy_id") is None
                    else strategy_names.get(str(row.get("strategy_id")), "UNMAPPED")
                ),
                "symbol_status": symbol_status,
                "symbol_reason": symbol_reason,
            }
        )
    position_rows = _objects(data.get("positions"), "positions")
    checks.append(
        _check(
            "Current positions preserve report attribution",
            position_rows == expected_position_rows
            and data.get("position_snapshots") == expected_position_snapshots
            and data["counts"]["positions"]
            == expected_position_snapshots["current_at_cutoff_rows"]
            and all(
                row.get("status") != "AVAILABLE_MAPPED"
                or (
                    isinstance(row.get("strategy_id"), str) and bool(row["strategy_id"])
                )
                for row in position_rows
            ),
            {
                "mapped": sum(
                    row.get("status") == "AVAILABLE_MAPPED" for row in position_rows
                ),
                "rows": len(position_rows),
                "source_rows": len(report_position_rows),
            },
        )
    )
    checks.append(
        _check(
            "Coverage-aware risk replay",
            data["advisory"]["hwm_status"] == data["hwm_summary"]["status"]
            and data["advisory"]["nav_coverage"] == data["hwm_summary"]["nav_coverage"]
            and data["advisory"]["readiness"] == "DO_NOT_EXPAND_SYSTEMATIC_RISK",
            data["advisory"],
        )
    )
    if report.get("strategy_provider_evidence"):
        strategy_rows = {
            _text(row.get("strategy_id"), "strategy strategy_id"): row
            for row in _current_strategy_rows(data)
        }
        volatis = strategy_rows.get("volatis-strategy", {})
        robuxio = strategy_rows.get("robuxio-crypto", {})
        p123 = strategy_rows.get("p123-ai-factor", {})
        volatis_evidence = _object(
            volatis.get("provider_evidence"), "Volatis provider evidence"
        )
        p123_evidence = _object(
            p123.get("provider_evidence"), "Portfolio123 provider evidence"
        )
        robuxio_evidence = _object(
            robuxio.get("provider_evidence"), "Robuxio provider evidence"
        )
        p123_account = _object(p123_evidence.get("account"), "P123 account evidence")
        p123_provider = _object(p123_evidence.get("provider"), "P123 provider evidence")
        synthetic_provider_shape = (
            len(strategy_rows) == len(current_strategy_ids)
            and set(strategy_rows) == current_strategy_ids
            and all(
                _object(row.get("provider_evidence"), "provider evidence").get(
                    "provider_id"
                )
                is not None
                for row in strategy_rows.values()
            )
        )
        checks.append(
            _check(
                "Provider evidence remains explicit and separate",
                synthetic_provider_shape
                or (
                    volatis.get("nav_status") == "AVAILABLE"
                    and volatis.get("hwm_source") == "PORTFOLIO_REPORT"
                    and volatis_evidence.get("provider_id") == "quantbase"
                    and robuxio.get("nav_status") == "AVAILABLE"
                    and robuxio.get("nav_evidence_layer") == "ACCOUNT_LIVE"
                    and robuxio.get("account_live_status") == "AVAILABLE"
                    and robuxio_evidence.get("provider_id") == "matrixport"
                    and isinstance(robuxio_evidence.get("account"), dict)
                    and p123_evidence.get("provider_id") == "ibkr"
                    and p123_account.get("observed_end")
                    != p123_provider.get("observed_end")
                    and isinstance(p123_evidence.get("overlap"), dict)
                    and isinstance(p123_evidence.get("current_snapshot"), dict)
                ),
                {
                    "p123_account_end": p123_account.get("observed_end"),
                    "p123_generation_as_of": p123_evidence.get("generation_as_of_date"),
                    "p123_provider_end": p123_provider.get("observed_end"),
                    "robuxio_layer": robuxio.get("nav_evidence_layer"),
                    "volatis_account_end": _object(
                        volatis_evidence.get("account"),
                        "Volatis account evidence",
                    ).get("observed_end"),
                },
            )
        )
    checks.append(
        _check(
            "Frozen candidates remain isolated",
            data["candidate_appendix"]["status"] == "FROZEN_AUDIT_APPENDIX"
            and len(data["candidate_appendix"]["providers"]) == 33
            and not any("candidate" in row for row in data["strategies"]),
            {
                "as_of": data["candidate_appendix"]["as_of_date"],
                "rows": len(data["candidate_appendix"]["providers"]),
            },
        )
    )
    driver_map = data.get("candidate_driver_map")
    if driver_map is not None:
        accepted_driver_map = _object(driver_map, "candidate driver map")
        driver_coverage = _object(
            accepted_driver_map.get("coverage"), "candidate driver-map coverage"
        )
        driver_authority = _object(
            accepted_driver_map.get("authority_boundary"),
            "candidate driver-map authority",
        )
        expected_driver_authority = {
            "allocation_effect": "NONE",
            "execution_effect": "NONE",
            "readiness_effect": "NONE",
            "automatic_add_drop_authority": "NONE",
        }
        checks.append(
            _check(
                "Candidate driver map is complete and advisory",
                driver_coverage.get("candidate_count") == 33
                and len(
                    _objects(
                        accepted_driver_map.get("driver_clusters"),
                        "candidate driver-map clusters",
                    )
                )
                == 5
                and len(
                    _objects(
                        accepted_driver_map.get("tactical_leave_one_out"),
                        "candidate tactical leave-one-out",
                    )
                )
                == 4
                and driver_authority == expected_driver_authority,
                {
                    "as_of": accepted_driver_map.get("as_of_date"),
                    "candidates": driver_coverage.get("candidate_count"),
                    "measured": driver_coverage.get("measured_candidate_count"),
                    "refresh": driver_coverage.get("refresh_status"),
                },
            )
        )
    checks.append(
        _check(
            "Incomplete facts remain explicit",
            bool(data.get("fact_coverage_matrix"))
            and (
                all(
                    row.get("status") == "COMPLETE_WITHIN_DECLARED_BOUNDARY"
                    or (
                        bool(row.get("known_gap"))
                        and bool(row.get("disposition"))
                        and bool(row.get("evidence_authority"))
                    )
                    for row in _objects(data.get("coverage"), "coverage")
                    if row.get("category") == "FACT_COVERAGE"
                )
                and all(
                    str(row.get("status", "")).startswith("PASS")
                    or (
                        bool(row.get("known_gap"))
                        and bool(row.get("disposition"))
                        and bool(row.get("resolution_authority"))
                    )
                    for row in _objects(data.get("coverage"), "coverage")
                    if row.get("category") == "RECONCILIATION_EXCEPTION"
                )
            ),
            {
                "fact_status": data["fact_status"],
                "retained_exceptions": len(data["blocking_items"]),
            },
        )
    )
    fact_matrix_rows = _objects(
        data.get("fact_coverage_matrix"), "fact coverage matrix"
    )
    no_path_rows = [
        row for row in fact_matrix_rows if row.get("status") == "NO_ACCEPTED_FACT_PATH"
    ]
    checks.append(
        _check(
            "Published evidence-scope sections are rendered",
            all(
                token in javascript
                for token in (
                    "data.fact_coverage_matrix",
                    "data.attribution_dispositions",
                    "data.observation_windows",
                    "proxyGovernance.selection_boundary",
                )
            )
            and all(
                token in html
                for token in (
                    'id="fact-coverage-matrix-table"',
                    'id="attribution-dispositions-table"',
                    'id="observation-windows-table"',
                    'id="factor-lens-boundary"',
                )
            )
            and bool(fact_matrix_rows),
            {
                "fact_coverage_rows": len(fact_matrix_rows),
                "no_accepted_fact_path_rows": len(no_path_rows),
                "attribution_dispositions": len(
                    _objects(
                        data.get("attribution_dispositions"),
                        "attribution dispositions",
                    )
                ),
                "observation_windows": len(
                    _objects(data.get("observation_windows"), "observation windows")
                ),
            },
        )
    )
    underwriting = _object(data.get("strategy_underwriting"), "strategy underwriting")
    underwriting_rows = _objects(underwriting.get("rows"), "strategy underwriting rows")
    authority_boundary = _object(
        underwriting.get("authority_boundary"), "underwriting authority boundary"
    )
    expected_layer_keys = {
        "design",
        "backtest",
        "provider_or_model_live",
        "account_live",
        "implementation_reconciliation",
    }
    implementation_findings = _check_implementation_layers(underwriting_rows)
    causal_contract_present = any(
        row.get("artifact") == "strategy-implementation-return-attribution"
        for row in _objects(data.get("provenance"), "panel provenance")
    )
    checks.append(
        _check(
            "Layered strategy underwriting is governed and advisory only",
            underwriting.get("policy_id") == "PORT-UNDERWRITE-1"
            and len(underwriting_rows) == len(current_strategy_ids)
            and {row.get("strategy_id") for row in underwriting_rows}
            == current_strategy_ids
            and all(
                set(
                    _object(
                        row.get("evidence_layers"),
                        "underwriting evidence layers",
                    )
                )
                == expected_layer_keys
                for row in underwriting_rows
            )
            and authority_boundary.get("automatic_drop_authority") == "NONE"
            and authority_boundary.get("allocation_authority") == "NONE"
            and authority_boundary.get("readiness_gate_effect") == "NONE"
            and authority_boundary.get("execution_authority") == "NONE"
            and all(
                _object(
                    row.get("advisory_disposition"),
                    "underwriting advisory disposition",
                ).get("allocation_effect")
                == "NONE"
                and _object(
                    row.get("advisory_disposition"),
                    "underwriting advisory disposition",
                ).get("execution_effect")
                == "NONE"
                for row in underwriting_rows
            )
            and implementation_findings["gap_reconciles"]
            and implementation_findings["causal_attribution_reconciles"]
            and implementation_findings["stale_shapes_valid"]
            and (
                not causal_contract_present
                or implementation_findings["implementation_strategy_ids"]
                == ["grizzlybears-vtma", "p123-ai-factor", "tt-portfolio"]
            ),
            {
                "policy_id": underwriting.get("policy_id"),
                "strategies": len(underwriting_rows),
                "authoritative_design": _object(
                    underwriting.get("counts"), "underwriting counts"
                ).get("authoritative_design"),
                "version_matched_long_history": _object(
                    underwriting.get("counts"), "underwriting counts"
                ).get("version_matched_long_history"),
                "quantified_model_account_gap": _object(
                    underwriting.get("counts"), "underwriting counts"
                ).get("quantified_model_account_gap"),
                "authority_boundary": authority_boundary,
                "gap_reconciles_without_fabricated_components": (
                    implementation_findings["gap_reconciles"]
                ),
                "causal_attribution_reconciles": (
                    implementation_findings["causal_attribution_reconciles"]
                ),
                "causal_attribution_strategy_ids": (
                    implementation_findings["causal_attribution_strategy_ids"]
                ),
                "policy_unavailable_strategy_ids": (
                    implementation_findings["policy_unavailable_strategy_ids"]
                ),
                "stale_attribution_strategy_ids": (
                    implementation_findings["stale_attribution_strategy_ids"]
                ),
                "stale_reconciliation_strategy_ids": (
                    implementation_findings["stale_reconciliation_strategy_ids"]
                ),
                "stale_shapes_valid": implementation_findings["stale_shapes_valid"],
            },
        )
    )
    factor_rows = _objects(data["factor_analysis"].get("series"), "factor.series")
    # The panel renders the default lens and its default source, not the top-level
    # `factor_analysis` alias (the dynamic-style lens), so that is what the check
    # validates: the rendered rows, their counts and their reconciliation.
    rendered_lens_id, rendered_source_key, rendered_rows = _rendered_default_lens_rows(
        lens_payload
    )
    rendered_counts = Counter(
        "available"
        if row.get("status") == "AVAILABLE"
        else "diagnostic_only"
        if str(row.get("status", "")).startswith("DIAGNOSTIC")
        else "unavailable"
        for row in rendered_rows
    )
    rendered_ids = {
        _text(row.get("series_id"), "rendered factor series_id")
        for row in rendered_rows
    }
    # A source-qualified lens (the current plan) lists every strategy under every
    # source, so the rendered source must cover the exact perimeter.  A combined
    # legacy lens spreads the strategies over its sources, so the rendered rows are a
    # subset there and the lens-wide perimeter check above covers the rest.
    rendered_perimeter_exact = (
        rendered_ids == required_analysis_series
        if _lens_is_source_qualified(
            _objects(_default_lens(lens_payload).get("series"), "default lens series")
        )
        else bool(rendered_rows) and rendered_ids <= required_analysis_series
    )
    default_lens = _default_lens(lens_payload)
    # The lens carries its own counts over every source it lists, so they only bind
    # the rendered rows when the lens is not narrowed to one source.
    rendered_counts_agree = rendered_source_key is not None or (
        default_lens.get("available_count"),
        default_lens.get("diagnostic_count"),
        default_lens.get("unavailable_count"),
    ) == (
        rendered_counts["available"],
        rendered_counts["diagnostic_only"],
        rendered_counts["unavailable"],
    )
    checks.append(
        _check(
            "Accepted factor and alpha evidence is exact",
            rendered_counts_agree
            and rendered_perimeter_exact
            and all(
                row.get("contribution_summary") is None
                or _finite_float(
                    _object(
                        row.get("contribution_summary"), "contribution_summary"
                    ).get("maximum_reconciliation_error"),
                    "maximum_reconciliation_error",
                )
                <= 1e-9
                for row in rendered_rows
            )
            and data["factor_analysis"]["available_count"]
            + data["factor_analysis"]["diagnostic_count"]
            + data["factor_analysis"]["unavailable_count"]
            == len(required_analysis_series)
            and {_text(row.get("series_id"), "factor series_id") for row in factor_rows}
            == required_analysis_series,
            {
                "available": rendered_counts["available"],
                "diagnostic_only": rendered_counts["diagnostic_only"],
                "evidence_mode": lens_payload.get("default_evidence_mode"),
                "generation": data["meta"]["decomposition_generation_id"],
                "lens_id": rendered_lens_id,
                "source_key": rendered_source_key,
                "unavailable": rendered_counts["unavailable"],
            },
        )
    )
    checks.append(
        _check(
            "Accepted dependence evidence is exact",
            data["dependence_analysis"]["pair_count"]
            == len(data["dependence_analysis"]["series_ids"])
            * (len(data["dependence_analysis"]["series_ids"]) - 1)
            // 2
            and data["dependence_analysis"]["available_pair_count"]
            == sum(
                _object(cell.get("observed"), "dependence observed cell").get("status")
                == "AVAILABLE"
                for cell in _objects(
                    data["dependence_analysis"].get("cells"), "dependence cells"
                )
            )
            and len(data["dependence_analysis"]["matrices"]) == 6
            and len(data["dependence_analysis"]["horizon_matrices"]) == 8
            and len(data["dependence_analysis"]["drawdown_matrices"]) == 2,
            {
                "available_pairs": data["dependence_analysis"]["available_pair_count"],
                "generation": data["meta"]["correlation_generation_id"],
                "pairs": data["dependence_analysis"]["pair_count"],
            },
        )
    )
    checks.append(
        _check(
            "Dependence evidence modes preserve live-only authority",
            data.get("default_dependence_mode") == "LIVE_ONLY"
            and _object(data.get("dependence_modes"), "dependence modes").get(
                "LIVE_ONLY"
            )
            == data["dependence_analysis"]
            and set(_object(data.get("dependence_modes"), "dependence modes"))
            <= {"LIVE_ONLY", "LIVE_PLUS_BACKTEST"},
            {
                "default": data.get("default_dependence_mode"),
                "modes": sorted(
                    _object(data.get("dependence_modes"), "dependence modes")
                ),
            },
        )
    )
    hybrid_gate = _object(data.get("hybrid_mode_gate"), "hybrid mode gate")
    hybrid_status = hybrid_gate.get("status")
    weekly_modes = set(
        _object(data.get("weekly_returns"), "weekly returns").get(
            "strategy_evidence_modes", []
        )
    )
    dependence_mode_names = set(
        _object(data.get("dependence_modes"), "dependence modes")
    )
    decomposition_mode_names = set(
        _object(
            _object(data.get("decomposition_lenses"), "decomposition lenses").get(
                "modes"
            ),
            "decomposition lens modes",
        )
    )
    # The verifier has to accept exactly what the builder publishes, including
    # partial coverage; see the note beside the builder's own gate.
    globally_hybrid_eligible = hybrid_status in {
        "ELIGIBLE_PROVEN_COMPATIBLE",
        "ELIGIBLE_PARTIAL_VERSION_COMPATIBILITY",
    }
    checks.append(
        _check(
            "Hybrid analytics fail closed on strategy-version compatibility",
            bool(hybrid_gate.get("reason"))
            and (
                (
                    globally_hybrid_eligible
                    and "LIVE_PLUS_BACKTEST" in dependence_mode_names
                    and (
                        weekly_returns_root is None
                        or "LIVE_PLUS_BACKTEST" in weekly_modes
                    )
                    and (
                        decomposition_lenses_root is None
                        or "LIVE_PLUS_BACKTEST" in decomposition_mode_names
                    )
                )
                or (
                    not globally_hybrid_eligible
                    and "LIVE_PLUS_BACKTEST" not in weekly_modes
                    and "LIVE_PLUS_BACKTEST" not in dependence_mode_names
                    and "LIVE_PLUS_BACKTEST" not in decomposition_mode_names
                )
            ),
            {
                "dependence_modes": sorted(dependence_mode_names),
                "decomposition_modes": sorted(decomposition_mode_names),
                "status": hybrid_gate.get("status"),
                "weekly_modes": sorted(weekly_modes),
            },
        )
    )
    result = {
        "all_passed": all(row["status"] == "PASS" for row in checks),
        "checks": checks,
        "checks_passed": sum(row["status"] == "PASS" for row in checks),
        "checks_total": len(checks),
        "panel_id": panel_id,
        "report_generation_id": data["meta"]["report_generation_id"],
    }
    if not result["all_passed"]:
        # Name the observed values, not just the check. Every one of these
        # checks already carries the detail that decided it, and printing only
        # the title turns a one-line diagnosis into a bisect.
        failed = "; ".join(
            "%s %s" % (row["check"], json.dumps(row.get("evidence"), sort_keys=True))
            for row in checks
            if row["status"] != "PASS"
        )
        raise ControlPanelValidationError(f"candidate verification failed: {failed}")
    return result


def _json_array(path: Path, field_name: str) -> list[object]:
    try:
        content, _sha256 = _read_snapshot(path, field_name)
        value = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControlPanelValidationError(f"{field_name} is invalid JSON") from exc
    if not isinstance(value, list):
        raise ControlPanelValidationError(f"{field_name} must be an array")
    return value


def _png_dimensions(path: Path) -> tuple[int, int] | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        header = path.read_bytes()[:24]
    except OSError:
        return None
    if (
        len(header) != 24
        or header[:8] != b"\x89PNG\r\n\x1a\n"
        or header[12:16] != b"IHDR"
    ):
        return None
    width = int.from_bytes(header[16:20], "big")
    height = int.from_bytes(header[20:24], "big")
    return (width, height) if width > 0 and height > 0 else None


# QA evidence manifests: version 2 carries relative locators only (F6).

QA_MANIFEST_VERSION: Final = 2
_QA_CAPTURE_FILES: Final = {
    "console_events_path": "console-events.json",
    "request_failures_path": "request-failures.json",
    "screenshot_path": "screenshot.png",
}
_QA_RECEIPT_NAMES: Final = frozenset(
    {"qa-validation.json", "stable-launcher-validation.json"}
)
_QA_TEXT_SUFFIXES: Final = frozenset(
    {".json", ".js", ".html", ".htm", ".css", ".txt", ".md", ".svg", ".csv"}
)
_QA_UNC_LOCATOR: Final = re.compile(rb"(?<![A-Za-z0-9_\\])(?:\\\\){1,2}[A-Za-z0-9_.$-]")
_QA_FILE_SCHEME: Final = re.compile(rb"(?<![A-Za-z0-9_])file:[/\\]", re.IGNORECASE)
_QA_SCHEME_OR_ROOT: Final = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*:|[/\\])")


def _is_ascii_letter(value: int) -> bool:
    return 65 <= value <= 90 or 97 <= value <= 122


def _is_ascii_word(value: int) -> bool:
    return _is_ascii_letter(value) or 48 <= value <= 57 or value == 95


def qa_absolute_locator_offsets(data: bytes, *, unc: bool = True) -> list[int]:
    r"""Offsets of physical locators in QA bytes.

    Refuses a drive locator (``X:\`` or ``X:/``, including inside ``file:///X:/``), a
    ``file:`` URL (``file:`` followed by a slash) and a UNC path that starts a token
    (raw ``\\host`` or JSON-escaped). The drive letter must start a token, so
    ``https://`` is not a hit. The producer owns this predicate and does not import the
    acceptance validator.
    """

    offsets: set[int] = set()
    for needle in (b":\\", b":/"):
        start = 0
        while True:
            index = data.find(needle, start)
            if index < 0:
                break
            start = index + 1
            if index < 1 or not _is_ascii_letter(data[index - 1]):
                continue
            if index >= 2 and _is_ascii_word(data[index - 2]):
                continue
            if needle == b":/" and data[index + 2 : index + 3] == b"/":
                continue
            offsets.add(index - 1)
    patterns = (_QA_FILE_SCHEME, _QA_UNC_LOCATOR) if unc else (_QA_FILE_SCHEME,)
    for pattern in patterns:
        offsets.update(match.start() for match in pattern.finditer(data))
    return sorted(offsets)


def _qa_pdf_streams(data: bytes) -> list[bytes]:
    streams: list[bytes] = []
    for match in re.finditer(rb"(?<!end)stream\r?\n", data):
        end = data.find(b"endstream", match.end())
        if end < 0:
            continue
        try:
            streams.append(zlib.decompressobj().decompress(data[match.end() : end]))
        except zlib.error:
            continue
    return streams


def qa_evidence_inventory_findings(qa_dir: Path) -> list[str]:
    """Name every absolute locator and misplaced receipt in one QA directory.

    This is the producer-side scan. It covers the text files the validator scans, the
    raw bytes and inflated streams of every PDF, the interaction result's retired
    ``external_manifest_path`` key, and any native-QA receipt stored inside ``qa/``.
    """

    findings: list[str] = []
    for path in sorted(qa_dir.rglob("*")):
        relative = path.relative_to(qa_dir).as_posix()
        if path.is_symlink():
            findings.append(f"{relative}: is a link")
            continue
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix in _QA_TEXT_SUFFIXES or suffix == ".pdf":
            data = path.read_bytes()
            is_pdf = suffix == ".pdf"
            # A text locator rule is meaningless inside successfully compressed
            # binary: the inflated content is what a reader sees. For a PDF,
            # scan each stream's inflated bytes; a stream that fails zlib is
            # scanned raw (its content is readable text); and the scaffolding
            # outside every stream region (URI actions, /Info, unfiltered
            # objects) is scanned raw too. The raw bytes of a successfully
            # inflated stream are never scanned: compressed bytes match
            # drive-letter shapes at random (the run-P false positive).
            hits: list[int] = []
            if is_pdf:
                stream_spans: list[tuple[int, int]] = []
                for match in re.finditer(rb"(?<!end)stream\r?\n", data):
                    end = data.find(b"endstream", match.end())
                    if end < 0:
                        continue
                    stream_spans.append((match.end(), end))
                    stream = data[match.end() : end]
                    try:
                        inflated = zlib.decompressobj().decompress(stream)
                    except zlib.error:
                        inflated = stream
                    hits.extend(qa_absolute_locator_offsets(inflated, unc=False))
                outside = bytearray()
                cursor = 0
                for span_start, span_end in stream_spans:
                    outside += data[cursor:span_start]
                    cursor = span_end
                outside += data[cursor:]
                hits.extend(qa_absolute_locator_offsets(bytes(outside), unc=False))
            else:
                hits = qa_absolute_locator_offsets(data, unc=True)
            if hits:
                findings.append(f"{relative}: {len(hits)} absolute locator(s)")
        if "/" not in relative and path.name.lower().startswith("native-qa"):
            findings.append(f"{relative}: native QA receipt stored inside qa/")
    interaction = qa_dir / "interaction-results.json"
    if interaction.is_file() and not interaction.is_symlink():
        try:
            parsed = json.loads(interaction.read_bytes())
        except ValueError:
            parsed = None
        checks = parsed.get("checks") if isinstance(parsed, dict) else None
        for check in checks if isinstance(checks, list) else []:
            evidence = check.get("evidence") if isinstance(check, dict) else None
            if isinstance(evidence, dict) and "external_manifest_path" in evidence:
                findings.append(
                    "interaction-results.json: external_manifest_path key present"
                )
    return findings


def _qa_scan_check(qa_dir: Path) -> dict[str, Any]:
    findings = [
        finding
        for finding in qa_evidence_inventory_findings(qa_dir)
        if finding.split(":", 1)[0] not in _QA_RECEIPT_NAMES
    ]
    return _check("QA evidence carries no absolute locator", not findings, findings)


def _qa_record_version(record: Mapping[str, Any], label: str) -> int:
    version = record.get("qa_manifest_version", 1)
    if isinstance(version, bool) or version not in (1, QA_MANIFEST_VERSION):
        raise ControlPanelValidationError(f"{label} has an unknown QA manifest version")
    return int(version)


def _qa_uniform_version(
    manifests: Mapping[str, Mapping[str, Any]], lints: Mapping[str, Mapping[str, Any]]
) -> int:
    versions = {
        _qa_record_version(record, f"{name} manifest")
        for name, record in manifests.items()
    } | {
        _qa_record_version(record, f"{name} visual lint")
        for name, record in lints.items()
    }
    if len(versions) != 1:
        raise ControlPanelValidationError(
            "QA evidence mixes manifest versions: "
            + ", ".join(str(version) for version in sorted(versions))
        )
    return next(iter(versions))


def _qa_capture_view(name: str) -> str:
    return "" if name == "direct-file" else name.split("-", 1)[1]


def _qa_relative_target(name: str) -> str:
    view = _qa_capture_view(name)
    return "index.html" + (f"#{view}" if view else "")


def _qa_paths_are_fixed(name: str, manifest: Mapping[str, Any]) -> bool:
    expected_pdf = "page.pdf" if name == "desktop-command" else None
    return (
        all(manifest.get(key) == value for key, value in _QA_CAPTURE_FILES.items())
        and manifest.get("pdf_path") == expected_pdf
    )


def validate_qa(
    candidate_dir: Path, qa_dir: Path, *, receipt_path: Path | None = None
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    candidate_record_path = candidate_dir / "candidate.json"
    candidate_record = _load_json(candidate_record_path, "candidate record")
    candidate_record_sha256 = _hash(candidate_record_path)
    candidate_artifact_sha256 = _object(
        candidate_record.get("artifact_sha256"), "candidate artifact_sha256"
    )
    screenshot_dimensions: dict[str, tuple[int, int] | None] = {}
    console_counts: dict[str, int] = {}
    request_counts: dict[str, int] = {}
    target_matches: dict[str, bool] = {}
    candidate_bindings: dict[str, bool] = {}
    manifests = {
        name: _load_json(qa_dir / name / "manifest.json", f"{name} manifest")
        for name in CAPTURES
    }
    lints = {
        name: _load_json(qa_dir / name / "visual-lint.json", f"{name} visual lint")
        for name in LINT_CAPTURES
    }
    qa_version = _qa_uniform_version(manifests, lints)
    for name in CAPTURES:
        capture = qa_dir / name
        screenshot = capture / "screenshot.png"
        screenshot_dimensions[name] = _png_dimensions(screenshot)
        console_counts[name] = len(
            _json_array(capture / "console-events.json", f"{name} console evidence")
        )
        request_counts[name] = len(
            _json_array(capture / "request-failures.json", f"{name} request evidence")
        )
        manifest = manifests[name]
        if qa_version == 1:
            target_matches[name] = candidate_dir.name in str(manifest.get("target", ""))
        else:
            relative_target = _qa_relative_target(name)
            target_matches[name] = (
                manifest.get("html_directory_name") == candidate_dir.name
                and manifest.get("target") == relative_target
                and manifest.get("final_url") == relative_target
                and lints.get(name, {"target": relative_target}).get("target")
                == relative_target
                and _qa_paths_are_fixed(name, manifest)
            )
        candidate_bindings[name] = (
            manifest.get("candidate_record_sha256") == candidate_record_sha256
            and manifest.get("artifact_sha256") == candidate_artifact_sha256
        )
    checks.extend(
        [
            _check(
                "Required browser screenshots exist",
                all(value is not None for value in screenshot_dimensions.values()),
                screenshot_dimensions,
            ),
            _check(
                "Browser captures have no console errors",
                all(value == 0 for value in console_counts.values()),
                console_counts,
            ),
            _check(
                "Browser captures have no request failures",
                all(value == 0 for value in request_counts.values()),
                request_counts,
            ),
            _check(
                "Browser evidence targets this candidate",
                all(target_matches.values()),
                target_matches,
            ),
            _check(
                "Browser evidence binds exact candidate artifacts",
                all(candidate_bindings.values()),
                candidate_bindings,
            ),
        ]
    )
    if qa_version == QA_MANIFEST_VERSION:
        checks.append(_qa_scan_check(qa_dir))
    lint_counts: dict[str, int] = {}
    for name in LINT_CAPTURES:
        count = lints[name].get("finding_count")
        if not isinstance(count, int):
            raise ControlPanelValidationError(f"{name} visual lint count is invalid")
        lint_counts[name] = count
    checks.append(
        _check(
            "Required desktop and mobile visual lint is clean",
            all(value == 0 for value in lint_counts.values()),
            lint_counts,
        )
    )
    interaction = _load_json(qa_dir / "interaction-results.json", "interaction QA")
    interaction_checks = _objects(interaction.get("checks"), "interaction.checks")
    interaction_names = {row.get("check") for row in interaction_checks}
    checks.append(
        _check(
            "Interaction suite passes",
            interaction.get("all_passed") is True
            and len(interaction_checks) >= 6
            and all(row.get("status") == "PASS" for row in interaction_checks),
            {
                "checks": len(interaction_checks),
                "candidate": interaction.get("panel_id"),
            },
        )
    )
    checks.append(
        _check(
            "Interaction evidence binds the candidate and epoch manifest",
            interaction.get("panel_id") == candidate_dir.name
            and interaction.get("candidate_record_sha256") == candidate_record_sha256
            and interaction.get("artifact_sha256") == candidate_artifact_sha256
            and "Epoch manifest DOM matches exact source values and blockers"
            in interaction_names,
            {
                "candidate": interaction.get("panel_id"),
                "epoch_check": (
                    "Epoch manifest DOM matches exact source values and blockers"
                    in interaction_names
                ),
            },
        )
    )
    checks.append(
        _check(
            "Direct-file runtime passes",
            console_counts["direct-file"] == 0
            and request_counts["direct-file"] == 0
            and screenshot_dimensions["direct-file"] is not None,
            "direct-file capture evidence",
        )
    )
    pdf = qa_dir / "desktop-command" / "page.pdf"
    try:
        pdf_header = pdf.read_bytes()[:5] if not pdf.is_symlink() else b""
    except OSError:
        pdf_header = b""
    checks.append(
        _check(
            "Command view PDF export exists",
            pdf.is_file() and pdf_header == b"%PDF-",
            pdf.stat().st_size if pdf.is_file() else 0,
        )
    )
    result = {
        "all_passed": all(row["status"] == "PASS" for row in checks),
        "checks": checks,
        "checks_passed": sum(row["status"] == "PASS" for row in checks),
        "checks_total": len(checks),
        "panel_id": candidate_dir.name,
    }
    if not result["all_passed"]:
        raise ControlPanelValidationError("browser QA failed")
    if receipt_path is None:
        _write_exact(qa_dir / "qa-validation.json", canonical_json_bytes(result))
    else:
        resolved_qa = qa_dir.resolve()
        resolved_receipt = receipt_path.resolve()
        if resolved_qa in (resolved_receipt, *resolved_receipt.parents):
            raise ControlPanelValidationError(
                "the native QA receipt must be written outside the QA directory"
            )
        _write_exact(receipt_path, canonical_json_bytes(result))
    return result


def validate_stable_launcher_qa(
    launcher_path: Path,
    qa_dir: Path,
    expected_generation_id: str,
) -> dict[str, Any]:
    """Prove that the stable file launcher resolves to one accepted generation."""

    if launcher_path.is_symlink() or not launcher_path.is_file():
        raise ControlPanelValidationError("stable launcher is missing or linked")
    launcher_text = launcher_path.read_text(encoding="utf-8")
    generation_fragment = f"generations/{expected_generation_id}/index.html"
    checks: list[dict[str, Any]] = [
        _check(
            "Stable launcher names the expected generation",
            launcher_text.count(generation_fragment) >= 3,
            expected_generation_id,
        )
    ]
    launcher_url = launcher_path.resolve().as_uri()
    expected_final_fragment = f"/generations/{expected_generation_id}/index.html"
    targets: dict[str, bool] = {}
    resolutions: dict[str, bool] = {}
    console_counts: dict[str, int] = {}
    request_counts: dict[str, int] = {}
    screenshot_sizes: dict[str, int] = {}
    launcher_manifests = {
        name: _load_json(qa_dir / name / "manifest.json", f"{name} manifest")
        for name in CAPTURES
    }
    launcher_lints = {
        name: _load_json(qa_dir / name / "visual-lint.json", f"{name} visual lint")
        for name in LINT_CAPTURES
    }
    qa_version = _qa_uniform_version(launcher_manifests, launcher_lints)
    if qa_version == QA_MANIFEST_VERSION:
        checks.append(_qa_scan_check(qa_dir))
    for name in CAPTURES:
        capture = qa_dir / name
        manifest = launcher_manifests[name]
        target = str(manifest.get("target", ""))
        final_url = str(manifest.get("final_url", ""))
        target_fragment = target.split("#", 1)[1] if "#" in target else ""
        final_fragment = final_url.split("#", 1)[1] if "#" in final_url else ""
        if qa_version == 1:
            targets[name] = target.split("#", 1)[0] == launcher_url
            resolutions[name] = (
                expected_final_fragment in final_url
                and target_fragment == final_fragment
            )
        else:
            target_path = target.split("#", 1)[0]
            final_path = final_url.split("#", 1)[0]
            relative_only = not any(
                _QA_SCHEME_OR_ROOT.match(value) for value in (target_path, final_path)
            )
            directory_name = manifest.get("html_directory_name")
            targets[name] = (
                relative_only
                and target_path != ""
                and (launcher_path.parent / target_path).resolve().as_uri()
                == launcher_url
                and target_fragment == _qa_capture_view(name)
                and isinstance(directory_name, str)
                and directory_name != ""
                and _qa_paths_are_fixed(name, manifest)
            )
            resolutions[name] = (
                relative_only
                and final_path == f"generations/{expected_generation_id}/index.html"
                and target_fragment == final_fragment
            )
        console_counts[name] = len(
            _json_array(capture / "console-events.json", f"{name} console evidence")
        )
        request_counts[name] = len(
            _json_array(capture / "request-failures.json", f"{name} request evidence")
        )
        screenshot = capture / "screenshot.png"
        screenshot_sizes[name] = (
            screenshot.stat().st_size if screenshot.is_file() else 0
        )
    checks.extend(
        [
            _check(
                "Every capture starts at the stable launcher",
                all(targets.values()),
                targets,
            ),
            _check(
                "Every capture resolves to the expected immutable generation",
                all(resolutions.values()),
                resolutions,
            ),
            _check(
                "Stable-launcher captures have no runtime failures",
                all(value == 0 for value in console_counts.values())
                and all(value == 0 for value in request_counts.values())
                and all(value > 0 for value in screenshot_sizes.values()),
                {
                    "console": console_counts,
                    "requests": request_counts,
                    "screenshots": screenshot_sizes,
                },
            ),
        ]
    )
    lint_counts: dict[str, int] = {}
    for name in LINT_CAPTURES:
        count = launcher_lints[name].get("finding_count")
        if not isinstance(count, int):
            raise ControlPanelValidationError(f"{name} visual lint count is invalid")
        lint_counts[name] = count
    checks.append(
        _check(
            "Stable-launcher visual lint is clean",
            all(value == 0 for value in lint_counts.values()),
            lint_counts,
        )
    )
    result = {
        "all_passed": all(row["status"] == "PASS" for row in checks),
        "checks": checks,
        "checks_passed": sum(row["status"] == "PASS" for row in checks),
        "checks_total": len(checks),
        "expected_generation_id": expected_generation_id,
        "launcher": str(launcher_path.resolve()),
        "launcher_sha256": hashlib.sha256(launcher_path.read_bytes()).hexdigest(),
    }
    if not result["all_passed"]:
        raise ControlPanelValidationError("stable launcher browser QA failed")
    _write_exact(
        qa_dir / "stable-launcher-validation.json", canonical_json_bytes(result)
    )
    return result


def _validated_qa_snapshots(
    candidate_dir: Path,
    candidate_snapshots: Mapping[str, bytes],
    qa_dir: Path,
    staging_parent: Path,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Validate and publish one private immutable snapshot of all QA evidence."""

    if qa_dir.is_symlink() or not qa_dir.is_dir():
        raise ControlPanelValidationError("QA evidence directory is missing or linked")
    source_snapshots: dict[str, bytes] = {}
    for path in sorted(qa_dir.rglob("*")):
        if path.is_symlink():
            raise ControlPanelValidationError("QA evidence contains a link")
        if path.is_file():
            source_snapshots[path.relative_to(qa_dir).as_posix()] = path.read_bytes()
    staging_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".qa-snapshot-", dir=staging_parent) as raw:
        snapshot_root = Path(raw)
        candidate_snapshot_dir = snapshot_root / candidate_dir.name
        snapshot_dir = snapshot_root / "qa"
        for relative, content in candidate_snapshots.items():
            _write_exact(candidate_snapshot_dir / relative, content)
        for relative, content in source_snapshots.items():
            _write_exact(snapshot_dir / relative, content)
        result = validate_qa(candidate_snapshot_dir, snapshot_dir)
        validated: dict[str, bytes] = {}
        for path in sorted(snapshot_dir.rglob("*")):
            if path.is_symlink():
                raise ControlPanelValidationError(
                    "validated QA snapshot contains a link"
                )
            if path.is_file():
                validated[path.relative_to(snapshot_dir).as_posix()] = path.read_bytes()
    return result, validated


def _replace_exact(path: Path, content: bytes) -> None:
    """Atomically replace one non-linked companion file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ControlPanelValidationError("publication companion cannot be a link")
    temporary = path.parent / f".{path.name}-{hashlib.sha256(content).hexdigest()}.tmp"
    try:
        _write_exact(temporary, content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


_NON_AVAILABLE_FACT_STATUSES: Final = frozenset({"FAILED", "PARTIAL", "UNAVAILABLE"})


def _coverage_boundary(fact_status: object) -> str:
    """Seal the payload's own non-available facts into the manifest boundary.

    A publishable manifest must be COMPLETE_WITHIN_DECLARED_BOUNDARY with no
    known_gaps, so the gaps the payload declares are named in the boundary text.
    """

    if not isinstance(fact_status, Mapping):
        raise ControlPanelValidationError("panel fact_status must be an object")
    declared = ", ".join(
        f"{key}={value}"
        for key, value in sorted(fact_status.items())
        if value in _NON_AVAILABLE_FACT_STATUSES
    )
    return (
        "accepted portfolio, factor/alpha, dynamic replication, dependence, "
        "weekly-return, governed-HWM, governed invested-capital when supplied, "
        "and candidate driver-map evidence rendered read-only with explicit gaps; "
        f"declared non-available facts: {declared or 'none'}"
    )


def _publish_current_launcher(output_root: Path, generation_id: str) -> str:
    """Atomically update the stable local entry point after pointer publication."""
    if not re.fullmatch(r"[0-9a-f]{64}", generation_id):
        raise ControlPanelValidationError("launcher generation ID is invalid")
    launcher = output_root / "index.html"
    if launcher.is_symlink():
        raise ControlPanelValidationError("stable launcher cannot be a link")
    target = f"generations/{generation_id}/index.html"
    content = (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8" />'
        '<meta name="viewport" content="width=device-width,initial-scale=1" />'
        '<meta name="robots" content="noindex,nofollow" />'
        '<meta http-equiv="refresh" content="0; url=' + target + '" />'
        "<title>V Capital — Current Portfolio Control</title></head>"
        "<body><main><h1>Opening the current accepted portfolio snapshot</h1>"
        "<p>This stable local launcher currently resolves to immutable generation "
        + generation_id
        + '.</p><p><a href="'
        + target
        + '">Open the accepted snapshot</a></p></main>'
        '<script>location.replace("./'
        + target
        + '"+location.hash);</script></body></html>\n'
    ).encode("utf-8")
    _replace_exact(launcher, content)
    return hashlib.sha256(content).hexdigest()


def _restore_panel_publication(
    output_root: Path,
    failed_generation_id: str,
    previous_pointer: Any,
    previous_launcher: bytes | None,
) -> None:
    """Restore the exact accepted pointer and launcher after a local failure."""

    try:
        current = read_current(output_root)
    except FileNotFoundError:
        current = None
    if current is not None and current.generation_id != failed_generation_id:
        if previous_pointer is None or current != previous_pointer:
            raise ControlPanelValidationError(
                "control-panel authority changed during recovery"
            )
    elif previous_pointer is None:
        current_path = output_root / "CURRENT.json"
        if current_path.is_symlink():
            raise ControlPanelValidationError("CURRENT.json cannot be a link")
        current_path.unlink(missing_ok=True)
    else:
        publish_generation(output_root, previous_pointer.generation_id)

    launcher = output_root / "index.html"
    if previous_launcher is None:
        if launcher.is_symlink():
            raise ControlPanelValidationError("stable launcher cannot be a link")
        launcher.unlink(missing_ok=True)
    else:
        _replace_exact(launcher, previous_launcher)


def publish_panel(
    report_root: Path,
    legacy_panel: Path,
    candidate_dir: Path,
    qa_dir: Path,
    output_root: Path,
    template_root: Path = DEFAULT_TEMPLATE_ROOT,
    decomposition_root: Path | None = None,
    correlation_root: Path | None = None,
    strategy_evidence_path: Path | None = None,
    decomposition_lenses_root: Path | None = None,
    decomposition_plan_bundle: Path | None = None,
    weekly_returns_root: Path | None = None,
    freshness_health_path: Path | None = None,
    replication_root: Path | None = None,
    candidate_driver_map_path: Path | None = None,
    baseline_root: Path | None = None,
    hwm_portable_receipt: Mapping[str, Any] | None = None,
    hwm_portable_generation_bytes: bytes | None = None,
    hwm_portable_calendar_bytes: bytes | None = None,
    hwm_governed_result: Mapping[str, Any] | None = None,
    hwm_governed_generation_bytes: bytes | None = None,
    hwm_governed_calendar_bytes: bytes | None = None,
    hwm_governed_receipt_bytes: bytes | None = None,
    account_manifest_path: Path | None = None,
    financial_mode: bool = False,
    manifest_ledger_path: Path | None = None,
    manifest_ledger_identity_path: Path | None = None,
    manifest_baseline_root: Path | None = None,
    manifest_classified_flows_path: Path | None = None,
    manifest_flow_coverage_path: Path | None = None,
    manifest_classification_policy_path: Path | None = None,
    manifest_admission_policy_path: Path | None = None,
    coordinated_release_receipt: Path | None = None,
    *,
    account_manifest_generation_root: Path | None = None,
    account_manifest_generation_id: str | None = None,
    manifest_membership_census_path: Path | None = None,
    invested_capital_result: Mapping[str, Any] | None = None,
    invested_capital_input_generation_path: Path | None = None,
    private_stage_root: Path | None = None,
    portfolio_root: Path | None = None,
    hybrid_decomposition_plan_bundle: Path | None = None,
    strategy_history_contract_path: Path | None = None,
    dependence_selected_inputs_path: Path | None = None,
) -> dict[str, Any]:
    governed_hwm_supplied = any(
        value is not None
        for value in (
            hwm_governed_result,
            hwm_governed_generation_bytes,
            hwm_governed_calendar_bytes,
            hwm_governed_receipt_bytes,
        )
    )
    financial_outcome_supplied = (
        governed_hwm_supplied or invested_capital_result is not None
    )
    has_generation_root = account_manifest_generation_root is not None
    has_generation_id = account_manifest_generation_id is not None
    if has_generation_root != has_generation_id:
        raise ControlPanelValidationError(
            "account manifest generation root and generation id must be supplied together"
        )
    verified_bundle_mode = has_generation_root and has_generation_id
    if financial_outcome_supplied or financial_mode or verified_bundle_mode:
        if private_stage_root is None or portfolio_root is None:
            raise ControlPanelValidationError(
                "governed financial publication requires private stage context"
            )
        validate_private_stage_target(output_root, private_stage_root, portfolio_root)
    if not financial_mode and not verified_bundle_mode:
        raise ControlPanelValidationError(
            "publication requires financial mode or an exact verified account-manifest generation bundle"
        )
    rendered_bundle: list[
        tuple[dict[str, bytes], dict[str, Any], dict[str, tuple[Path, str]]]
    ] = []
    verification = verify_candidate(
        report_root,
        legacy_panel,
        candidate_dir,
        template_root,
        decomposition_root,
        correlation_root,
        strategy_evidence_path,
        decomposition_lenses_root,
        decomposition_plan_bundle,
        weekly_returns_root,
        freshness_health_path,
        replication_root,
        candidate_driver_map_path,
        baseline_root,
        hwm_portable_receipt,
        hwm_portable_generation_bytes,
        hwm_portable_calendar_bytes,
        hwm_governed_result,
        hwm_governed_generation_bytes,
        hwm_governed_calendar_bytes,
        hwm_governed_receipt_bytes,
        account_manifest_path=account_manifest_path,
        financial_mode=financial_mode,
        manifest_ledger_path=manifest_ledger_path,
        manifest_ledger_identity_path=manifest_ledger_identity_path,
        manifest_baseline_root=manifest_baseline_root,
        manifest_classified_flows_path=manifest_classified_flows_path,
        manifest_flow_coverage_path=manifest_flow_coverage_path,
        manifest_classification_policy_path=manifest_classification_policy_path,
        manifest_admission_policy_path=manifest_admission_policy_path,
        account_manifest_generation_root=account_manifest_generation_root,
        account_manifest_generation_id=account_manifest_generation_id,
        manifest_membership_census_path=manifest_membership_census_path,
        invested_capital_result=invested_capital_result,
        invested_capital_input_generation_path=(invested_capital_input_generation_path),
        hybrid_decomposition_plan_bundle=hybrid_decomposition_plan_bundle,
        strategy_history_contract_path=strategy_history_contract_path,
        dependence_selected_inputs_path=dependence_selected_inputs_path,
        _rendered_bundle=rendered_bundle,
    )
    if verification.get("all_passed") is not True:
        raise ControlPanelValidationError(
            "publication requires a candidate reproduced from the selected authority"
        )
    qa = validate_qa(candidate_dir, qa_dir)
    _write_exact(
        qa_dir / "semantic-validation.json", canonical_json_bytes(verification)
    )
    if len(rendered_bundle) != 1:
        raise ControlPanelValidationError("verified render bundle is unavailable")
    rendered_files, reproduced_data, input_paths = rendered_bundle[0]
    reproduced_meta = _object(reproduced_data.get("meta"), "panel meta")
    authority_profile = reproduced_meta.get("account_manifest_authority_profile")
    manifest_verification = _object(
        reproduced_meta.get("account_manifest_verification"),
        "account manifest verification",
    )
    financial_authority = (
        authority_profile == MANIFEST_AUTHORITY_FINANCIAL
        and manifest_verification.get("publication_readiness") == "READY"
    )
    verified_bundle_authority = False
    if authority_profile == MANIFEST_AUTHORITY_VERIFIED_BUNDLE:
        producer_readiness = _object(
            manifest_verification.get("producer_readiness"),
            "verified bundle producer readiness",
        )
        verified_bundle_authority = (
            verified_bundle_mode
            and manifest_verification.get("status")
            == "REPORT_ATTESTED_TO_EXACT_BUNDLE_BYTES"
            and manifest_verification.get("method")
            == "READ_GENERATION_BUNDLE_EXACT_BYTES_AND_FINANCIAL_MVP_BINDER"
            and manifest_verification.get("publication_readiness") == "NOT_READY"
            and producer_readiness.get("independent_authority_verification")
            == "NOT_PERFORMED"
            and producer_readiness.get("publication_readiness") == "NOT_READY"
            and isinstance(manifest_verification.get("epoch_manifest_source"), dict)
            and isinstance(manifest_verification.get("independent_report"), dict)
        )
    if not (financial_authority or verified_bundle_authority):
        raise ControlPanelValidationError(
            "publication requires an admitted independent exact manifest authority profile"
        )
    published_panel_files = (*rendered_files, "candidate.json")
    reproduced_candidate_record = canonical_json_bytes(
        _candidate_record(rendered_files, reproduced_data)
    )
    published_panel_bytes: dict[str, bytes] = {}
    for relative in published_panel_files:
        path = candidate_dir / relative
        snapshot, _snapshot_sha256 = _read_snapshot(
            path, f"published candidate artifact: {relative}"
        )
        expected_snapshot = (
            reproduced_candidate_record
            if relative == "candidate.json"
            else rendered_files.get(relative)
        )
        if expected_snapshot != snapshot:
            raise ControlPanelValidationError(
                f"candidate changed after verification or source snapshot drifted: {relative}"
            )
        published_panel_bytes[relative] = snapshot
    try:
        data = _object(
            json.loads(
                published_panel_bytes["data/dashboard_data.json"].decode("utf-8")
            ),
            "published candidate panel data",
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControlPanelValidationError(
            "published candidate panel data is invalid JSON"
        ) from exc
    _validate_panel_data_schema(data)
    input_snapshots: dict[str, bytes] = {}
    for name, (path, expected_sha256) in input_paths.items():
        snapshot, _snapshot_sha256 = _read_snapshot(
            path, f"bound publication input: {name}"
        )
        if hashlib.sha256(snapshot).hexdigest() != expected_sha256:
            raise ControlPanelValidationError(
                f"publication input changed after final render: {name}"
            )
        input_snapshots[name] = snapshot
    portfolio_as_of = date.fromisoformat(
        _text(data["meta"]["portfolio_as_of"], "as_of")
    )
    try:
        freshness_artifact = _object(
            json.loads(input_snapshots["freshness"].decode("utf-8")),
            "freshness health artifact",
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControlPanelValidationError(
            "freshness health snapshot is invalid JSON"
        ) from exc
    freshness_as_of = date.fromisoformat(
        _text(
            freshness_artifact.get("as_of_date"),
            "freshness health artifact as_of_date",
        )
    )
    # The manifest has one date field and downstream readers take it as the data date
    # (weekly anchor, retained cutoff, provider ledger), so it is the data cutoff the
    # panel carries, never the later date the run executes on.  The execution date is
    # recorded inside the freshness artifact as ``evaluated_on``.
    if freshness_as_of > portfolio_as_of:
        raise ControlPanelValidationError(
            "freshness health is dated after the portfolio data cutoff"
        )
    publication_as_of = portfolio_as_of
    try:
        retained_report = _object(
            json.loads(input_snapshots["report"].decode("utf-8")),
            "bound publication report snapshot",
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControlPanelValidationError(
            "bound publication report snapshot is invalid JSON"
        ) from exc
    retained_report_schema_version = retained_report.get("report_schema_version")
    if isinstance(
        retained_report_schema_version, bool
    ) or retained_report_schema_version not in {1, 2, 3, 4, 5}:
        raise ControlPanelValidationError(
            "bound publication report schema is unsupported"
        )

    def snapshot_artifact(
        binding: str,
        *,
        artifact_id: str,
        as_of_date: date,
        schema_name: str,
        schema_version: str,
    ) -> SourceArtifact:
        snapshot = input_snapshots[binding]
        return SourceArtifact(
            artifact_id=artifact_id,
            as_of_date=as_of_date,
            schema_name=schema_name,
            schema_version=schema_version,
            sha256=hashlib.sha256(snapshot).hexdigest(),
            size_bytes=len(snapshot),
        )

    artifacts: list[SourceArtifact] = [
        *(
            [
                SourceArtifact(
                    artifact_id="input/account-strategy-manifest.json",
                    as_of_date=publication_as_of,
                    schema_name="account-strategy-manifest",
                    schema_version=str(data["account_manifest"]["schema_version"]),
                    sha256=_text(
                        data["meta"]["account_manifest_sha256"],
                        "account manifest snapshot hash",
                    ),
                    size_bytes=data["meta"]["account_manifest_size_bytes"],
                )
            ]
            if account_manifest_path is not None
            or account_manifest_generation_root is not None
            else []
        ),
        snapshot_artifact(
            "report",
            artifact_id="input/portfolio-report-data.json",
            as_of_date=portfolio_as_of,
            schema_name="portfolio-report-data",
            schema_version=str(retained_report_schema_version),
        ),
        snapshot_artifact(
            "legacy",
            artifact_id="input/frozen-candidate-appendix.json",
            as_of_date=date.fromisoformat(data["candidate_appendix"]["as_of_date"]),
            schema_name="strategy-allocation-audit-candidates",
            schema_version="frozen-2026-07-17",
        ),
        snapshot_artifact(
            "decomposition",
            artifact_id="input/portfolio-factor-decomposition.json",
            as_of_date=date.fromisoformat(data["factor_analysis"]["as_of_date"]),
            schema_name="portfolio-factor-decomposition",
            schema_version="1",
        ),
        snapshot_artifact(
            "correlation",
            artifact_id="input/portfolio-correlation.json",
            as_of_date=date.fromisoformat(data["dependence_analysis"]["as_of_date"]),
            schema_name="portfolio-correlation",
            schema_version="1",
        ),
        snapshot_artifact(
            "hwm_policy",
            artifact_id="input/portfolio-hwm-policy.json",
            as_of_date=publication_as_of,
            schema_name="portfolio-hwm-policy",
            schema_version="2",
        ),
        snapshot_artifact(
            "economic_driver_clusters",
            artifact_id="input/portfolio-economic-driver-clusters.json",
            as_of_date=publication_as_of,
            schema_name="portfolio-economic-driver-clusters",
            schema_version="1",
        ),
        snapshot_artifact(
            "underwriting_policy",
            artifact_id="input/portfolio-strategy-underwriting-policy.json",
            as_of_date=publication_as_of,
            schema_name="portfolio-strategy-underwriting-policy",
            schema_version="1",
        ),
    ]
    if "candidate_driver_map" in input_snapshots:
        driver_map = _object(data.get("candidate_driver_map"), "candidate driver map")
        artifacts.append(
            snapshot_artifact(
                "candidate_driver_map",
                artifact_id="input/portfolio-candidate-driver-map.json",
                as_of_date=date.fromisoformat(
                    _text(driver_map.get("as_of_date"), "driver-map as_of_date")
                ),
                schema_name="portfolio-driver-map",
                schema_version="1",
            )
        )
    if (
        "strategy_evidence" in input_snapshots
        and data["meta"].get("strategy_evidence_source") == "LEGACY_COMPATIBILITY_INPUT"
    ):
        artifacts.append(
            snapshot_artifact(
                "strategy_evidence",
                artifact_id="input/current-strategy-provider-evidence.json",
                as_of_date=date.fromisoformat(
                    _text(data["meta"]["strategy_evidence_as_of"], "evidence as_of")
                ),
                schema_name="current-strategy-provider-evidence",
                schema_version="1",
            )
        )
    lens_bindings = sorted(
        name for name in input_snapshots if name.startswith("decomposition_lens:")
    )
    for index, binding in enumerate(lens_bindings):
        artifacts.append(
            snapshot_artifact(
                binding,
                artifact_id=f"input/decomposition-lens-{index:02d}.json",
                as_of_date=portfolio_as_of,
                schema_name="portfolio-decomposition-lens-input",
                schema_version="1",
            )
        )
    if "weekly_returns" in input_snapshots:
        artifacts.append(
            snapshot_artifact(
                "weekly_returns",
                artifact_id="input/portfolio-weekly-returns.json",
                as_of_date=date.fromisoformat(data["weekly_returns"]["as_of_date"]),
                schema_name="portfolio-weekly-returns",
                schema_version="2",
            )
        )
    if "replication" in input_snapshots:
        artifacts.append(
            snapshot_artifact(
                "replication",
                artifact_id="input/portfolio-dynamic-replication.json",
                as_of_date=date.fromisoformat(
                    data["replication_analysis"]["as_of_date"]
                ),
                schema_name="portfolio-dynamic-replication",
                schema_version="1",
            )
        )
    artifacts.append(
        snapshot_artifact(
            "freshness",
            artifact_id="input/freshness-health.json",
            as_of_date=freshness_as_of,
            schema_name="freshness-assessment",
            schema_version=str(freshness_artifact["assessment_schema_version"]),
        )
    )
    specialized_bindings = {
        "account_manifest",
        "candidate_driver_map",
        "correlation",
        "decomposition",
        "economic_driver_clusters",
        "freshness",
        "hwm_policy",
        "legacy",
        "replication",
        "report",
        "strategy_evidence",
        "underwriting_policy",
        "weekly_returns",
        *lens_bindings,
    }
    for binding in sorted(set(input_snapshots) - specialized_bindings):
        artifacts.append(
            snapshot_artifact(
                binding,
                artifact_id=f"input-bound/{binding.replace(':', '/')}",
                as_of_date=publication_as_of,
                schema_name="portfolio-control-panel-bound-input",
                schema_version="1",
            )
        )
    for relative in published_panel_files:
        content = published_panel_bytes[relative]
        artifacts.append(
            SourceArtifact(
                artifact_id=f"panel/{relative}",
                as_of_date=publication_as_of,
                schema_name="portfolio-control-panel-artifact",
                schema_version="1",
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
            )
        )
    after_cutoff = sorted(
        item.artifact_id for item in artifacts if item.as_of_date > publication_as_of
    )
    if after_cutoff:
        raise ControlPanelValidationError(
            "bound inputs are dated after the portfolio data cutoff: "
            + ", ".join(after_cutoff)
        )
    qa, qa_snapshots = _validated_qa_snapshots(
        candidate_dir, published_panel_bytes, qa_dir, output_root
    )
    # QA can take materially longer than the final render.  Recheck every
    # authority after validating the private QA snapshot so publication cannot
    # combine fresh QA with stale or swapped financial inputs.
    for name, (path, _expected_sha256) in input_paths.items():
        snapshot, _snapshot_sha256 = _read_snapshot(
            path, f"bound publication input: {name}"
        )
        if snapshot != input_snapshots[name]:
            raise ControlPanelValidationError(
                f"publication input changed after QA snapshot: {name}"
            )
    for relative, content in sorted(qa_snapshots.items()):
        artifacts.append(
            SourceArtifact(
                artifact_id=f"evidence/{relative}",
                as_of_date=publication_as_of,
                schema_name="portfolio-control-panel-qa",
                schema_version="1",
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
            )
        )
    manifest = GenerationManifest.build(
        as_of_date=publication_as_of,
        adapter=AdapterIdentity("portfolio-control-panel-builder", BUILDER_VERSION),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary=_coverage_boundary(data["fact_status"]),
            fact_types=(
                "portfolio control panel",
                "factor and alpha attribution",
                "dynamic style research replication",
                "correlation and dependence",
                "weekly returns by strategy, factor, style, sector, industry, and benchmark",
                "governed HWM coverage",
                *(
                    ("governed invested capital",)
                    if data.get("invested_capital") is not None
                    else ()
                ),
                "browser QA",
                "frozen candidate evidence",
            ),
            observed_start=portfolio_as_of,
            observed_end=publication_as_of,
        ),
        artifacts=tuple(artifacts),
        verification=VerificationStatus.VERIFIED,
    )
    manifest_path = stage_generation(output_root, manifest)
    generation_dir = manifest_path.parent
    for relative in published_panel_files:
        _write_exact(generation_dir / relative, published_panel_bytes[relative])
    for relative, content in sorted(qa_snapshots.items()):
        _write_exact(generation_dir / "qa" / relative, content)
    receipt_sha256, release_id = _validated_coordinated_release_receipt(
        coordinated_release_receipt, output_root, candidate_dir, qa_dir
    )
    try:
        previous_pointer = read_current(output_root)
    except FileNotFoundError:
        previous_pointer = None
    launcher_path = output_root / "index.html"
    if launcher_path.is_symlink():
        raise ControlPanelValidationError("stable launcher cannot be a link")
    previous_launcher = launcher_path.read_bytes() if launcher_path.exists() else None
    try:
        pointer = publish_generation(output_root, manifest.generation_id)
        launcher_hash = _publish_current_launcher(output_root, manifest.generation_id)
    except Exception:
        _restore_panel_publication(
            output_root,
            manifest.generation_id,
            previous_pointer,
            previous_launcher,
        )
        raise
    return {
        "browser_checks": qa["checks_total"],
        "correlation_generation_id": data["meta"]["correlation_generation_id"],
        "decomposition_generation_id": data["meta"]["decomposition_generation_id"],
        "generation_id": manifest.generation_id,
        "launcher_sha256": launcher_hash,
        "panel_id": verification["panel_id"],
        "publication_state": pointer.state.value,
        "report_generation_id": verification["report_generation_id"],
        "release_id": release_id,
        "coordinated_release_receipt_sha256": receipt_sha256,
        "weekly_returns_generation_id": data["meta"]["weekly_returns_generation_id"],
        "stable_launcher": str(output_root / "index.html"),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("build", "verify", "publish"):
        command = commands.add_parser(name)
        command.add_argument("--report-root", type=Path)
        if name != "publish":
            command.add_argument(
                "--report-candidate",
                type=Path,
                help=(
                    "Hash-addressed local portfolio-report-data.json candidate; "
                    "build and verify only, never a published CURRENT generation"
                ),
            )
        command.add_argument(
            "--strategy-history-contract",
            type=Path,
            help=(
                "Packaged dependence-source.json matching the report's "
                "strategy-history contract.  Required with --report-candidate, "
                "and on a published report whenever it declares one: publish "
                "recomputes the numerical HWM through the same path as build "
                "and verify, and needs that source's epoch HPR receipts."
            ),
        )
        command.add_argument("--dependence-selected-inputs", type=Path)
        command.add_argument("--legacy-panel", type=Path, required=True)
        command.add_argument(
            "--template-root", type=Path, default=DEFAULT_TEMPLATE_ROOT
        )
        command.add_argument("--decomposition-root", type=Path)
        command.add_argument("--decomposition-lenses-root", type=Path)
        command.add_argument("--decomposition-plan-bundle", type=Path)
        command.add_argument("--hybrid-decomposition-plan-bundle", type=Path)
        command.add_argument("--correlation-root", type=Path)
        command.add_argument("--weekly-returns-root", type=Path)
        command.add_argument("--replication-root", type=Path)
        command.add_argument("--candidate-driver-map", type=Path)
        command.add_argument(
            "--account-manifest",
            type=Path,
            help=(
                "Account x strategy epoch manifest JSON (formal + semantic "
                "validation; financial mode independently rebuilds it)"
            ),
        )
        command.add_argument(
            "--account-manifest-generation-root",
            type=Path,
            help=(
                "Verified account-manifest generation root; requires "
                "--account-manifest-generation-id and cannot be combined with "
                "legacy account-manifest inputs"
            ),
        )
        command.add_argument(
            "--account-manifest-generation-id",
            help=(
                "Verified account-manifest generation id; requires "
                "--account-manifest-generation-root"
            ),
        )
        command.add_argument(
            "--financial-mode",
            action="store_true",
            help=(
                "Require a v5 epoch manifest and independently selected "
                "producer inputs; intended for financial release wiring."
            ),
        )
        command.add_argument(
            "--manifest-ledger",
            type=Path,
            help="Selected provider ledger used to independently rebuild the manifest",
        )
        command.add_argument(
            "--manifest-ledger-identity",
            type=Path,
            help="Selected ledger SourceArtifact identity used by the manifest",
        )
        command.add_argument(
            "--manifest-baseline-root",
            type=Path,
            help="Selected baseline root used to independently rebuild the manifest",
        )
        command.add_argument(
            "--manifest-classified-flows",
            type=Path,
            help="Selected classified-flow artifact used by the manifest",
        )
        command.add_argument(
            "--manifest-flow-coverage",
            type=Path,
            help="Selected account-and-interval flow-completeness authority",
        )
        command.add_argument(
            "--manifest-classification-policy",
            type=Path,
            help="Separately selected flow-classification policy authority",
        )
        command.add_argument(
            "--manifest-admission-policy",
            type=Path,
            help="Selected admission-policy artifact used by the manifest",
        )
        command.add_argument(
            "--manifest-membership-census-json",
            type=Path,
            help=(
                "Selected governed membership-census artifact used by the "
                "manifest (defaults to the repository census contract)"
            ),
        )
        command.add_argument("--strategy-evidence", type=Path)
        command.add_argument("--freshness-health", type=Path, required=True)
        command.add_argument(
            "--baseline-root",
            type=Path,
            required=True,
            help=(
                "Governed baseline generation root (reconciliation authority "
                "for the HWM envelope, crosswalk, and perimeter)"
            ),
        )
        command.add_argument("--output-root", type=Path, required=name != "verify")
        if name != "build":
            command.add_argument("--candidate-dir", type=Path, required=True)
        command.add_argument("--hwm-governed-result", type=Path)
        command.add_argument(
            "--hwm-governed-input-generation",
            "--hwm-governed-generation",
            dest="hwm_governed_input_generation",
            type=Path,
        )
        command.add_argument(
            "--hwm-governed-calendar-authority",
            "--hwm-governed-calendar",
            dest="hwm_governed_calendar_authority",
            type=Path,
        )
        command.add_argument(
            "--hwm-governed-admission-receipt",
            "--hwm-governed-receipt",
            dest="hwm_governed_admission_receipt",
            type=Path,
        )
        command.add_argument("--invested-capital-result", type=Path)
        command.add_argument("--invested-capital-input-generation", type=Path)
        if name == "publish":
            command.add_argument("--qa-dir", type=Path, required=True)
            command.add_argument(
                "--coordinated-release-receipt", type=Path, required=True
            )
            command.add_argument("--private-stage-root", type=Path)
            command.add_argument("--portfolio-root", type=Path)
    qa = commands.add_parser("qa")
    qa.add_argument("--candidate-dir", type=Path, required=True)
    qa.add_argument("--qa-dir", type=Path, required=True)
    qa.add_argument(
        "--receipt",
        type=Path,
        help="write the native QA receipt here, outside --qa-dir (default: inside it)",
    )
    qa_inventory = commands.add_parser("qa-inventory")
    qa_inventory.add_argument("--qa-dir", type=Path, required=True)
    launcher_qa = commands.add_parser("launcher-qa")
    launcher_qa.add_argument("--launcher", type=Path, required=True)
    launcher_qa.add_argument("--qa-dir", type=Path, required=True)
    launcher_qa.add_argument("--expected-generation-id", required=True)
    return parser


def _load_optional_json_arg(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    parsed = json.loads(path.read_text(encoding="utf-8"))
    return parsed if isinstance(parsed, dict) else None


def _governed_hwm_cli_values(args: argparse.Namespace) -> dict[str, Any]:
    paths = (
        args.hwm_governed_result,
        args.hwm_governed_input_generation,
        args.hwm_governed_calendar_authority,
        args.hwm_governed_admission_receipt,
    )
    if all(path is None for path in paths):
        return {}
    if any(path is None for path in paths):
        raise ControlPanelValidationError(
            "governed HWM paths must be supplied together"
        )
    return {
        "hwm_governed_result": _load_optional_json_arg(paths[0]),
        "hwm_governed_generation_bytes": paths[1].read_bytes(),
        "hwm_governed_calendar_bytes": paths[2].read_bytes(),
        "hwm_governed_receipt_bytes": paths[3].read_bytes(),
    }


def _invested_capital_cli_values(args: argparse.Namespace) -> dict[str, Any]:
    result_path = args.invested_capital_result
    input_path = args.invested_capital_input_generation
    if result_path is None and input_path is None:
        return {}
    if result_path is None or input_path is None:
        raise ControlPanelValidationError(
            "invested-capital paths must be supplied together"
        )
    return {
        "invested_capital_result": _load_optional_json_arg(result_path),
        "invested_capital_input_generation_path": input_path,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            if (args.report_root is None) == (args.report_candidate is None):
                raise ControlPanelValidationError(
                    "build requires exactly one of --report-root or --report-candidate"
                )
            if (
                args.report_candidate is not None
                and args.strategy_history_contract is None
            ):
                raise ControlPanelValidationError(
                    "build with --report-candidate requires --strategy-history-contract"
                )
            result = build_candidate(
                args.report_root,
                args.legacy_panel,
                args.output_root,
                args.template_root,
                args.decomposition_root,
                args.correlation_root,
                args.strategy_evidence,
                args.decomposition_lenses_root,
                args.decomposition_plan_bundle,
                args.weekly_returns_root,
                args.freshness_health,
                args.replication_root,
                args.candidate_driver_map,
                args.baseline_root,
                account_manifest_path=args.account_manifest,
                financial_mode=args.financial_mode,
                manifest_ledger_path=args.manifest_ledger,
                manifest_ledger_identity_path=args.manifest_ledger_identity,
                manifest_baseline_root=args.manifest_baseline_root,
                manifest_classified_flows_path=args.manifest_classified_flows,
                manifest_flow_coverage_path=args.manifest_flow_coverage,
                manifest_classification_policy_path=(
                    args.manifest_classification_policy
                ),
                manifest_admission_policy_path=args.manifest_admission_policy,
                manifest_membership_census_path=(args.manifest_membership_census_json),
                report_candidate=args.report_candidate,
                strategy_history_contract_path=args.strategy_history_contract,
                account_manifest_generation_root=(
                    args.account_manifest_generation_root
                ),
                account_manifest_generation_id=args.account_manifest_generation_id,
                **_governed_hwm_cli_values(args),
                **_invested_capital_cli_values(args),
                hybrid_decomposition_plan_bundle=args.hybrid_decomposition_plan_bundle,
                dependence_selected_inputs_path=args.dependence_selected_inputs,
            )
        elif args.command == "verify":
            if (args.report_root is None) == (args.report_candidate is None):
                raise ControlPanelValidationError(
                    "verify requires exactly one of --report-root or --report-candidate"
                )
            if (
                args.report_candidate is not None
                and args.strategy_history_contract is None
            ):
                raise ControlPanelValidationError(
                    "verify with --report-candidate requires --strategy-history-contract"
                )
            result = verify_candidate(
                args.report_root,
                args.legacy_panel,
                args.candidate_dir,
                args.template_root,
                args.decomposition_root,
                args.correlation_root,
                args.strategy_evidence,
                args.decomposition_lenses_root,
                args.decomposition_plan_bundle,
                args.weekly_returns_root,
                args.freshness_health,
                args.replication_root,
                args.candidate_driver_map,
                args.baseline_root,
                account_manifest_path=args.account_manifest,
                financial_mode=args.financial_mode,
                manifest_ledger_path=args.manifest_ledger,
                manifest_ledger_identity_path=args.manifest_ledger_identity,
                manifest_baseline_root=args.manifest_baseline_root,
                manifest_classified_flows_path=args.manifest_classified_flows,
                manifest_flow_coverage_path=args.manifest_flow_coverage,
                manifest_classification_policy_path=(
                    args.manifest_classification_policy
                ),
                manifest_admission_policy_path=args.manifest_admission_policy,
                manifest_membership_census_path=(args.manifest_membership_census_json),
                report_candidate=args.report_candidate,
                strategy_history_contract_path=args.strategy_history_contract,
                account_manifest_generation_root=(
                    args.account_manifest_generation_root
                ),
                account_manifest_generation_id=args.account_manifest_generation_id,
                **_governed_hwm_cli_values(args),
                **_invested_capital_cli_values(args),
                hybrid_decomposition_plan_bundle=args.hybrid_decomposition_plan_bundle,
                dependence_selected_inputs_path=args.dependence_selected_inputs,
            )
        elif args.command == "qa":
            result = validate_qa(
                args.candidate_dir, args.qa_dir, receipt_path=args.receipt
            )
        elif args.command == "qa-inventory":
            findings = qa_evidence_inventory_findings(args.qa_dir)
            result = {"all_passed": not findings, "findings": findings}
            if findings:
                raise ControlPanelValidationError(
                    "QA inventory scan failed: " + "; ".join(findings)
                )
        elif args.command == "launcher-qa":
            result = validate_stable_launcher_qa(
                args.launcher, args.qa_dir, args.expected_generation_id
            )
        else:
            if args.report_root is None:
                raise ControlPanelValidationError("publish requires --report-root")
            result = publish_panel(
                args.report_root,
                args.legacy_panel,
                args.candidate_dir,
                args.qa_dir,
                args.output_root,
                args.template_root,
                args.decomposition_root,
                args.correlation_root,
                args.strategy_evidence,
                args.decomposition_lenses_root,
                args.decomposition_plan_bundle,
                args.weekly_returns_root,
                args.freshness_health,
                args.replication_root,
                args.candidate_driver_map,
                args.baseline_root,
                account_manifest_path=args.account_manifest,
                financial_mode=args.financial_mode,
                manifest_ledger_path=args.manifest_ledger,
                manifest_ledger_identity_path=args.manifest_ledger_identity,
                manifest_baseline_root=args.manifest_baseline_root,
                manifest_classified_flows_path=args.manifest_classified_flows,
                manifest_flow_coverage_path=args.manifest_flow_coverage,
                manifest_classification_policy_path=(
                    args.manifest_classification_policy
                ),
                manifest_admission_policy_path=args.manifest_admission_policy,
                manifest_membership_census_path=(args.manifest_membership_census_json),
                account_manifest_generation_root=(
                    args.account_manifest_generation_root
                ),
                account_manifest_generation_id=args.account_manifest_generation_id,
                coordinated_release_receipt=args.coordinated_release_receipt,
                hwm_portable_receipt=_load_optional_json_arg(
                    getattr(args, "hwm_portable_receipt", None)
                ),
                **_governed_hwm_cli_values(args),
                **_invested_capital_cli_values(args),
                **(
                    {
                        "private_stage_root": args.private_stage_root,
                        "portfolio_root": args.portfolio_root,
                    }
                    if args.private_stage_root is not None
                    or args.portfolio_root is not None
                    else {}
                ),
                hybrid_decomposition_plan_bundle=args.hybrid_decomposition_plan_bundle,
                strategy_history_contract_path=args.strategy_history_contract,
                dependence_selected_inputs_path=args.dependence_selected_inputs,
            )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"portfolio-control-panel: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
