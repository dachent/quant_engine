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
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
ACCOUNT_RE = re.compile(r"account-([0-9a-f]{16})")
ASSIGNMENT_RE = re.compile(r"assignment-([0-9a-f]{20})")
TXN_RE = re.compile(r"txn-([0-9a-f]{24})")
HEX64_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")
OWNER_IN_TEXT_RE = re.compile('(?P<pre>•\d{4} — [^()"\n]+? )\((?P<owner>[A-Z][a-z]+)\)')
FOUR_IN_TEXT_RE = re.compile(r"\b(IBKR|Schwab|Fidelity|Alpaca) (•)?(\d{4})\b")
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
    def __init__(self, salt: str, ids: frozenset[str] | None = None) -> None:
        # ``ids``: the real ids to remap (``account-<hex16>``); None remaps every id
        self._ids = ids
        self._salt = salt.encode("utf-8")
        digest = self._mac("amount-scale")
        self.scale = Decimal(6000 + int(digest[:8], 16) % 8000) / Decimal(10000)

    def _mac(self, text: str) -> str:
        return hmac.new(self._salt, text.encode("utf-8"), hashlib.sha256).hexdigest()

    def account(self, hex16: str, force: bool = False) -> str:
        if not force and self._ids is not None and "account-" + hex16 not in self._ids:
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


def synthesize_rows(source: Path, mapper: Mapper) -> bytes:
    document = json.loads(source.read_text(encoding="ascii"))
    out = _walk(document, mapper)
    if isinstance(out, dict) and isinstance(out.get("provenance"), dict):
        out["provenance"]["synthetic_note"] = (
            "synthetic stand-in generated by tests/acceptance/make_synthetic_fixtures.py "
            "(issue 696); ids, labels and USD amounts are remapped, dimensionless "
            "ratios are unchanged; the real originals are in the governed data root"
        )
    text = json.dumps(out, indent=1, ensure_ascii=True) + "\n"
    return text.encode("ascii")


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
        args.salt_file.read_text(encoding="utf-8").strip(), frozenset(spec["ids"])
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
        for rel in spec["row_files"]:
            new = synthesize_rows(args.rows_source_dir / Path(rel).name, mapper)
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
