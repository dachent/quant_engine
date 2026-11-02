"""Issue 696: derive synthetic stand-ins for test fixtures that carried real values.

Policy: Git holds synthetic fixtures only.  Real portfolio ids and amounts live in
the governed data root.  This script is the one deterministic path from a real
original to its synthetic stand-in; nothing here is hand-edited.

Inputs that are NOT in Git (all under the governed data root):

* ``--spec``  a JSON file naming the real ids, real amount tokens (global and per file)
  and the files to rewrite.  It is the only place the real tokens are written down.
* ``--salt-file``  one line of secret text.  Every synthetic identifier and the single
  amount scale factor derive from it with HMAC-SHA256, so the mapping cannot be
  inverted from the committed output alone.

What it does:

* identifiers: ``account-<16 hex>`` -> ``account-<16 hex of HMAC(salt, real id)>``;
  last-four labels and the owner names inside account labels are remapped the same way
* amounts: every spec amount token (and every USD amount field in the row fixtures) is
  multiplied by ONE factor ``K`` in ``[0.6, 1.4)``, computed exactly in ``Decimal``.  A
  single factor keeps every sum, ratio, XIRR and cost-rate relationship the tests assert
* dimensionless ratios (daily returns, TWR, MWR, beta, ...) are left as they are in the
  row fixtures: the validators recompute them from the amounts, and the tests pin them

Files are read and written as bytes so LF endings survive on Windows.
Run it only on pristine originals (the id and last-four rewrites are not idempotent), then
``ruff format`` the rewritten files.

    python tests/acceptance/make_synthetic_fixtures.py --spec <spec.json> --salt-file <salt> \
        [--rows-source-dir <governed dir with the real row fixtures>] [--check]
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import sys
from datetime import date
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
ACCOUNT_RE = re.compile(r"account-([0-9a-f]{16})")
ASSIGNMENT_RE = re.compile(r"assignment-([0-9a-f]{20})")
TXN_RE = re.compile(r"txn-([0-9a-f]{24})")
HEX64_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")
OWNER_IN_TEXT_RE = re.compile(
    r"(?P<pre>\u2022\d{4} \u2014 [^()\"\n]+? )\((?P<owner>[A-Z][a-z]+)\)"
)
FOUR_IN_TEXT_RE = re.compile(r"\b(IBKR|Schwab|Fidelity|Alpaca) (\u2022)?(\d{4})\b")
MONEY_KEYS = frozenset(
    {
        "amount",
        "beginning_capital",
        "current_aggregate_usd",
        "invested_capital_usd",
        "mwr_opening_value",
        "mwr_terminal_value",
        "net_capital",
        "opening_capital",
        "performance_opening_value",
        "value",
    }
)
DECIMAL_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


class Mapper:
    def __init__(
        self,
        salt: str,
        ids: frozenset[str] | None = None,
        keep: frozenset[str] = frozenset(),
    ) -> None:
        # ``ids``: the real ids to remap (``account-<hex16>``); None remaps every id.
        # ``keep``: ids deliberately left real (production code still names them); any
        # other id met in a governed input stops the generation instead of leaking.
        self._ids = ids
        self._keep = keep
        self._salt = salt.encode("utf-8")
        digest = self._mac("amount-scale")
        self.scale = Decimal(6000 + int(digest[:8], 16) % 8000) / Decimal(10000)

    def _mac(self, text: str) -> str:
        return hmac.new(self._salt, text.encode("utf-8"), hashlib.sha256).hexdigest()

    def account(self, hex16: str, force: bool = False) -> str:
        if not force and self._ids is not None and "account-" + hex16 not in self._ids:
            if "account-" + hex16 not in self._keep:
                raise ValueError(
                    f"unlisted account id in a governed input: account-{hex16}"
                )
            return "account-" + hex16
        return "account-" + self._mac("account-" + hex16)[:16]

    def assignment(self, hex20: str) -> str:
        return "assignment-" + self._mac("assignment-" + hex20)[:20]

    def txn(self, hex24: str) -> str:
        return "txn-" + self._mac("txn-" + hex24)[:24]

    def hex64(self, value: str) -> str:
        return self._mac("hex64-" + value)

    def four(self, digits: str) -> str:
        return f"{int(self._mac('four-' + digits)[:8], 16) % 10000:04d}"

    def owner(self, name: str) -> str:
        return "Owner" + str(int(self._mac("owner-" + name)[:4], 16) % 90 + 10)

    def money(self, token: str) -> str:
        with localcontext() as ctx:
            ctx.prec = 80
            text = format(Decimal(token) * self.scale, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text

    @staticmethod
    def plain_displays(token: str) -> list[str]:
        value = Decimal(token)
        if abs(value) < 1000:
            return []
        return [f"{value:,.{places}f}" for places in (0, 1, 2)]

    def displays(self, token: str) -> list[tuple[str, str]]:
        """(real display, synthetic display) pairs for a token as ``$1,234[.56]`` text."""

        pairs: list[tuple[str, str]] = []
        with localcontext() as ctx:
            ctx.prec = 80
            real, scaled = Decimal(token), Decimal(token) * self.scale
            for places in (0, 1, 2):
                if abs(real) < 1000:
                    continue
                pairs.append((f"{real:,.{places}f}", f"{scaled:,.{places}f}"))
        return pairs

    def labels(self, value: str) -> str:
        """Owner names and last-four digits inside account labels."""

        value = OWNER_IN_TEXT_RE.sub(
            lambda m: f"{m.group('pre')}({self.owner(m.group('owner'))})", value
        )
        return FOUR_IN_TEXT_RE.sub(
            lambda m: f"{m.group(1)} {m.group(2) or ''}{self.four(m.group(3))}", value
        )

    def text(self, value: str) -> str:
        value = ACCOUNT_RE.sub(lambda m: self.account(m.group(1)), value)
        value = ASSIGNMENT_RE.sub(lambda m: self.assignment(m.group(1)), value)
        value = TXN_RE.sub(lambda m: self.txn(m.group(1)), value)
        value = HEX64_RE.sub(lambda m: self.hex64(m.group(0)), value)
        value = self.labels(value)
        return value


def _walk(node: Any, mapper: Mapper, key: str = "") -> Any:
    if isinstance(node, dict):
        return {k: _walk(v, mapper, k) for k, v in node.items()}
    if isinstance(node, list):
        return [_walk(v, mapper, key) for v in node]
    if isinstance(node, str):
        if key == "last_four" and DECIMAL_RE.match(node):
            return mapper.four(node)
        if key in MONEY_KEYS and DECIMAL_RE.match(node):
            return mapper.money(node)
        if key == "run":
            return "synthetic rows derived from the governed originals (issue 696)"
        return mapper.text(node)
    return node


SERIES = {
    # strategy -> (daily mean, daily sigma, clamp) of the synthetic return series
    "volatis-strategy": (Decimal("0.0009"), Decimal("0.004"), Decimal("0.0099")),
    "p123-eu": (Decimal("0.0005"), Decimal("0.003"), Decimal("0.0099")),
}
FORCED_DROP = {("volatis-strategy", "2025-11-13"): (Decimal("0.034"), Decimal("0.018"))}


def _dec(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _fraction(mapper: Mapper, label: str) -> Decimal:
    return Decimal(int(mapper._mac("frac-" + label)[:8], 16)) / Decimal(16**8)


def _between(mapper: Mapper, label: str, low: str, high: str) -> Decimal:
    span = Decimal(high) - Decimal(low)
    return (Decimal(low) + span * _fraction(mapper, label)).quantize(Decimal("1e-15"))


def _normal(mapper: Mapper, label: str) -> Decimal:
    digest = mapper._mac("z-" + label)
    ints = [int(digest[i : i + 4], 16) for i in range(0, 48, 4)]
    return Decimal(sum(ints)) / Decimal(65536) - 6


def _series(
    mapper: Mapper,
    strategy: str,
    rows: list[dict[str, Any]],
    flows: list[tuple[str, Decimal]],
) -> dict[str, Decimal]:
    """Rewrite the daily rows in place with a synthetic capital-identity series.

    ``bc[k+1] = nav[k] + flows recognised by day k+1`` and ``return[k] = nav[k] /
    bc[k] - 1``, the identity the validators check; returns are drawn from the salt.
    """

    mean, sigma, clamp = SERIES[strategy]
    navs: dict[str, Decimal] = {}
    with localcontext() as ctx:
        ctx.prec = 40
        capital = Decimal(rows[0]["beginning_capital"])
        pending = sorted(flows)
        index = 0
        for position, row in enumerate(rows):
            day = row["date"]
            if position:
                while index < len(pending) and pending[index][0] <= day:
                    capital += pending[index][1]
                    index += 1
            if Decimal(row["return"]) == 0:
                value = Decimal(0)
            elif (strategy, day) in FORCED_DROP:
                low, span = FORCED_DROP[(strategy, day)]
                value = -(low + span * _fraction(mapper, f"{strategy}-{day}"))
            else:
                value = mean + sigma * _normal(mapper, f"{strategy}-{day}")
                value = max(-clamp, min(clamp, value))
            value = value.quantize(Decimal("1e-15"))
            nav = capital * (1 + value)
            row["beginning_capital"] = _dec(capital)
            row["return"] = _dec(value)
            navs[day] = nav
            capital = nav
    return navs


def _compound(values: list[Decimal]) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 40
        total = Decimal(1)
        for value in values:
            total *= 1 + value
        return total - 1


def _window(
    rows: list[dict[str, Any]], start: str | None, end: str | None
) -> list[Decimal]:
    return [
        Decimal(row["return"])
        for row in rows
        if row.get("evidence_layer") == "ACCOUNT_LIVE"
        and (start is None or row["date"] >= start)
        and (end is None or row["date"] <= end)
    ]


def _synthetic_returns(document: dict[str, Any], mapper: Mapper) -> dict[str, Decimal]:
    from quant_engine.portfolio_ledger.returns import xirr

    series = document["strategy_returns"]
    traces = {
        row["strategy_id"]: row.get("capital_flow_trace") or []
        for row in document["flow_manifest"]
    }
    navs: dict[str, Decimal] = {}
    for strategy, rows in series.items():
        flows = [
            (item["nav_recognition_date"], Decimal(item["amount"]))
            for item in traces.get(strategy, [])
            if item.get("nav_recognition_date")
        ]
        found = _series(mapper, strategy, rows, flows)
        if strategy == "volatis-strategy":
            navs = found
    # the dimensionless benchmark statistics
    for strategy, comparison in document["benchmark_comparisons"].items():
        metrics = comparison["metrics"]
        selected = _window(
            series[strategy], metrics["observed_start"], metrics["observed_end"]
        )
        cumulative = _compound(selected)
        share = _between(mapper, f"{strategy}-reference", "0.35", "0.9")
        reference = cumulative * share
        metrics["account_cumulative_return"] = _dec(cumulative)
        metrics["observations"] = len(selected)
        metrics["reference_cumulative_return"] = _dec(reference)
        metrics["cumulative_gap"] = _dec(cumulative - reference)
        for name, low, high in (
            ("active_return_annualized", "-0.05", "0.25"),
            ("beta", "0.4", "1.8"),
            ("correlation", "0.3", "0.8"),
            ("downside_capture", "0.6", "1.5"),
            ("information_ratio", "-0.4", "0.9"),
            ("relative_drawdown", "-0.3", "-0.05"),
            ("tracking_error_annualized", "0.05", "0.4"),
            ("upside_capture", "0.6", "1.7"),
        ):
            metrics[name] = _dec(_between(mapper, f"{strategy}-{name}", low, high))
        document["strategy_summary"][strategy][
            "benchmark_account_cumulative_return"
        ] = _dec(cumulative)
    # the epoch rows: terminal value, XIRR and TWR recomputed from synthetic inputs
    for table in ("tt_twr_mwr", "flow_twr_mwr"):
        for position, row in enumerate(document[table]):
            flows = row.get("mwr_cashflows") or []
            label = f"{table}-{position}-{row['strategy_id']}"
            if flows and row.get("mwr_terminal_value") is not None:
                factor = _between(mapper, label + "-terminal", "0.85", "1.2")
                with localcontext() as ctx:
                    ctx.prec = 40
                    terminal = (Decimal(flows[-1]["amount"]) * factor).quantize(
                        Decimal("1e-6")
                    )
                flows[-1]["amount"] = _dec(terminal)
                row["mwr_terminal_value"] = _dec(terminal)
                pairs = [
                    (date.fromisoformat(item["date"]), Decimal(item["amount"]))
                    for item in flows
                ]
                value = xirr(pairs)
                row["mwr_xirr"] = None if value is None else format(value, "f")
            if row.get("twr") is not None:
                strategy = row["strategy_id"]
                if strategy in series:
                    selected = _window(
                        series[strategy], row.get("effective_start"), None
                    )
                    cumulative = _compound(selected)
                    row["return_path_observations"] = len(selected)
                else:
                    cumulative = _between(mapper, label + "-twr", "-0.04", "0.3")
                row["twr"] = _dec(cumulative)
                row["return_path_cumulative_return"] = _dec(cumulative)
                if row.get("return_path_series_sha256") is not None:
                    row["return_path_series_sha256"] = mapper.hex64(label)
    return navs


def build_rows(source_dir: Path, mapper: Mapper) -> dict[str, bytes]:
    """The synthetic row fixtures, keyed by file name."""

    out: dict[str, bytes] = {}
    document = _walk(
        json.loads((source_dir / "smoke9_real_rows.json").read_text(encoding="ascii")),
        mapper,
    )
    navs = _synthetic_returns(document, mapper)
    document["provenance"]["synthetic_note"] = (
        "synthetic stand-in generated by tests/acceptance/make_synthetic_fixtures.py "
        "(issue 696): ids, labels, USD amounts and every return series are "
        "synthetic (returns, XIRR and TWR recomputed from synthetic capital paths); "
        "the real originals are in the governed data root"
    )
    out["smoke9_real_rows.json"] = (
        json.dumps(document, indent=1, ensure_ascii=True) + "\n"
    ).encode("ascii")
    raw = _walk(
        json.loads(
            (source_dir / "a6_volatis_raw_source_rows.json").read_text(encoding="ascii")
        ),
        mapper,
    )
    flows = {
        item["nav_recognition_date"]: Decimal(item["amount"])
        for row in document["flow_manifest"]
        if row["strategy_id"] == "volatis-strategy"
        for item in row["capital_flow_trace"]
    }
    for observation in raw["nav_observations"]:
        value = navs[observation["as_of_date"]]
        for field in ("market_value", "nav"):
            observation[field]["value"] = _dec(value)
    for anchor in raw["capital_anchors"]:
        anchor["amount"]["value"] = _dec(flows[anchor["nav_recognition_date"]])
    if "note" in raw:
        raw["note"] = (
            "synthetic raw nav_observations and capital_anchors rows (issue 696), "
            "consistent with the synthetic strategy_returns series"
        )
    out["a6_volatis_raw_source_rows.json"] = (
        json.dumps(raw, indent=1, ensure_ascii=True) + "\n"
    ).encode("ascii")
    return out


def _token_pattern(token: str, display: bool = False) -> re.Pattern[str]:
    if display:
        return re.compile(r"(?<![\d.,])" + re.escape(token) + r"(?!\d|\.\d|,\d)")
    return re.compile(r"(?<![\d.])" + re.escape(token) + r"(?!\d|\.\d)")


def scrub_python(path: Path, mapper: Mapper, spec: dict[str, Any]) -> bytes:
    text = path.read_bytes().decode("utf-8")
    try:
        rel = path.relative_to(REPO).as_posix()
    except ValueError:
        rel = path.as_posix()
    extra_ids = spec.get("file_ids", {}).get(rel, [])
    for real in spec["ids"]:
        text = text.replace(real, mapper.account(real.removeprefix("account-")))
    for real in extra_ids:
        text = text.replace(real, mapper.account(real.removeprefix("account-"), True))
    text = mapper.labels(text)
    # exact replacements for derived values (sums and differences that involve a
    # replaced amount); the spec computes them so identities keep holding
    exact = {**spec.get("replace", {}), **spec.get("file_replace", {}).get(rel, {})}
    for token, new in sorted(exact.items(), key=lambda kv: len(kv[0]), reverse=True):
        text = _token_pattern(token).sub(new, text)
        old_forms, new_forms = mapper.plain_displays(token), mapper.plain_displays(new)
        for real_display, new_display in (
            zip(old_forms, new_forms) if old_forms and new_forms else ()
        ):
            text = _token_pattern(real_display, True).sub(new_display, text)
    extra = spec.get("file_tokens", {}).get(rel, [])
    for token in sorted([*spec["amount_tokens"], *extra], key=len, reverse=True):
        scaled = mapper.money(token)
        text = _token_pattern(token).sub(scaled, text)
        for real_display, scaled_display in mapper.displays(token):
            text = _token_pattern(real_display, True).sub(scaled_display, text)
    return text.encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--salt-file", type=Path, required=True)
    parser.add_argument("--rows-source-dir", type=Path)
    parser.add_argument("--check", action="store_true", help="report, do not write")
    args = parser.parse_args(argv)
    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    mapper = Mapper(
        args.salt_file.read_text(encoding="utf-8").strip(),
        frozenset(spec["ids"]),
        frozenset(spec.get("keep_ids", [])),
    )
    changed = 0
    for rel in spec["python_files"]:
        path = REPO / rel
        new = scrub_python(path, mapper, spec)
        if new != path.read_bytes():
            changed += 1
            print(("would rewrite " if args.check else "rewrote ") + rel)
            if not args.check:
                path.write_bytes(new)
    if args.rows_source_dir is not None:
        built = build_rows(args.rows_source_dir, mapper)
        for rel in spec["row_files"]:
            new = built[Path(rel).name]
            target = REPO / rel
            if not target.exists() or new != target.read_bytes():
                changed += 1
                print(("would write " if args.check else "wrote ") + rel)
                if not args.check:
                    target.write_bytes(new)
    print(f"{changed} file(s) differ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
