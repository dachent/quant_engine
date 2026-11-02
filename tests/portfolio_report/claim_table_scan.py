"""Find static sentences that make a claim and have no claim-table row.

The scanner reads the shipped renderer (``app.js``, ``index.html``) and the
Python text builders, extracts string literals that read as prose, splits them
into sentences and keeps the ones that carry a claim pattern: a universal
quantifier, a count, a basis word, an epoch / week / evidence-mode word or a
coverage / status word.  A sentence is *covered* when a claim-table needle
matches inside it, *allowed* when the allowlist (with a justification) names
it, and *uncovered* otherwise.  Error messages, docstrings and comments are
not reader-facing text and are not scanned.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import html
import re
from pathlib import Path

SCAN_FILES: tuple[str, ...] = (
    "apps/portfolio-control-panel/app.js",
    "apps/portfolio-control-panel/index.html",
    "tools/portfolio_control_panel.py",
    "tools/portfolio_report.py",
    "tools/portfolio_baseline.py",
    "tools/portfolio_totals.py",
    "tools/portfolio_model_vs_actual.py",
    "tools/portfolio_performance_tab.py",
)

PATTERNS: dict[str, re.Pattern[str]] = {
    "universal": re.compile(
        r"\b(every|all|each|none|never|always|only|remains?|any|both|neither|no)\b",
        re.I,
    ),
    "count": re.compile(
        r"(\{\}|\b\d[\d,]*)\s+(of\s+(\{\}|\d)|strategies|epochs?|accounts?|rows?|days?|d\b|"
        r"facts?|weeks?|providers?|candidates?|positions?)",
        re.I,
    ),
    "basis": re.compile(
        r"\b(parent|sleeve|sleeves|account basis|account-live)\b", re.I
    ),
    "epoch": re.compile(
        r"\b(epochs?|weeks?|weekly|LIVE_ONLY|LIVE_PLUS_BACKTEST|evidence mode|mixed)\b",
        re.I,
    ),
    "coverage": re.compile(
        r"\b(complete|partial|coverage|covered|unavailable)\b", re.I
    ),
}
_SENTENCE_END = re.compile(r"(?<=[.;!?])\s+|\n")
_TAG = re.compile(r"<[^>]*>")
_WORDS = re.compile(r"[A-Za-z][A-Za-z'’-]*")
_DIAGNOSTIC = re.compile(r"(Error|console\.\w+)\(\s*$")
_CODEISH = re.compile(
    r"=>|\bfunction\b|querySelector|\.map\(|\bconst\b|\breturn\b|\{\{|\}\}"
)


@dataclass(frozen=True)
class Literal:
    """One prose-looking string: ``text`` and, per character, a source offset."""

    file: str
    text: str
    offsets: tuple[int, ...]


@dataclass(frozen=True)
class Sentence:
    file: str
    start: int  # source offset (start of the first character)
    end: int  # source offset (end of the last character)
    text: str
    kinds: tuple[str, ...]

    @property
    def key(self) -> str:
        return f"{self.file}::{normalise(self.text)}"


def normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# --- JavaScript -------------------------------------------------------------


def js_literals(file: str, source: str) -> list[Literal]:
    """String and template literals of a JavaScript source (comments skipped)."""

    out: list[Literal] = []
    n = len(source)
    i = 0
    prev = ""  # last significant non-space character outside strings/comments

    def read_string(quote: str, start: int) -> tuple[int, str, list[int]]:
        j = start + 1
        text: list[str] = []
        offs: list[int] = []
        while j < n and source[j] != quote:
            if source[j] == "\\" and j + 1 < n:
                text.append(source[j + 1])
                offs.append(j)
                j += 2
                continue
            if source[j] == "\n":
                break
            text.append(source[j])
            offs.append(j)
            j += 1
        return j + 1, "".join(text), offs

    def read_template(start: int) -> tuple[int, str, list[int]]:
        j = start + 1
        text: list[str] = []
        offs: list[int] = []
        while j < n and source[j] != "`":
            ch = source[j]
            if ch == "\\" and j + 1 < n:
                text.append(source[j + 1])
                offs.append(j)
                j += 2
            elif ch == "$" and source.startswith("${", j):
                depth = 1
                k = j + 2
                while k < n and depth:
                    c = source[k]
                    if c == "`":  # nested template: skip it whole
                        k, _t, _o = read_template(k)
                        continue
                    if c in "'\"":
                        k, _t, _o = read_string(c, k)
                        continue
                    depth += (c == "{") - (c == "}")
                    k += 1
                text.extend("{}")
                offs.extend([j, j])
                j = k
            else:
                text.append(ch)
                offs.append(j)
                j += 1
        return j + 1, "".join(text), offs

    while i < n:
        ch = source[i]
        if ch == "/" and source.startswith("//", i):
            i = source.find("\n", i)
            i = n if i < 0 else i
            continue
        if ch == "/" and source.startswith("/*", i):
            end = source.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        if ch in "'\"" or ch == "`":
            diagnostic = bool(_DIAGNOSTIC.search(source[max(0, i - 40) : i]))
            if ch == "`":
                i, text, offs = read_template(i)
            else:
                i, text, offs = read_string(ch, i)
            if not diagnostic:
                out.append(Literal(file, text, tuple(offs)))
            prev = "x"
            continue
        if ch == "/" and prev in "(,=:[!&|?{};\n":
            j = i + 1
            in_class = False
            while j < n and source[j] != "\n":
                if source[j] == "\\":
                    j += 2
                    continue
                if source[j] == "[":
                    in_class = True
                elif source[j] == "]":
                    in_class = False
                elif source[j] == "/" and not in_class:
                    break
                j += 1
            i = j + 1
            prev = "x"
            continue
        if not ch.isspace():
            prev = ch
        i += 1
    return out


# --- HTML -------------------------------------------------------------------


def html_literals(file: str, source: str) -> list[Literal]:
    body = re.sub(
        r"<(script|style)\b.*?</\1>",
        lambda m: " " * len(m.group(0)),
        source,
        flags=re.S | re.I,
    )
    out: list[Literal] = []
    for match in re.finditer(r"[^<>]+", body):
        chunk = match.group(0)
        if len(_WORDS.findall(chunk)) >= 5:
            out.append(Literal(file, chunk, tuple(range(match.start(), match.end()))))
    return out


# --- Python -----------------------------------------------------------------


def py_literals(file: str, source: str) -> list[Literal]:
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))

    def offset(lineno: int, col: int) -> int:
        # ast columns are UTF-8 byte offsets
        line = lines[lineno - 1]
        return starts[lineno - 1] + len(
            line.encode("utf-8")[:col].decode("utf-8", "ignore")
        )

    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Raise, ast.Assert)):
            skip.update(id(sub) for sub in ast.walk(node))
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
            ):
                skip.update(id(sub) for sub in ast.walk(body[0]))
        if isinstance(node, ast.Dict):
            skip.update(id(key) for key in node.keys if key is not None)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in {"print", "getattr", "hasattr", "isinstance"}:
                skip.update(id(sub) for sub in ast.walk(node))
    out: list[Literal] = []
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        text: str | None = None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
        elif isinstance(node, ast.JoinedStr):
            text = "".join(
                part.value if isinstance(part, ast.Constant) else "{}"
                for part in node.values
            )
        if text is None or node.end_lineno is None or node.end_col_offset is None:
            continue
        # a JoinedStr's inner constants are visited too; skip them
        if isinstance(node, ast.JoinedStr):
            skip.update(id(part) for part in node.values)
        start = offset(node.lineno, node.col_offset)
        end = offset(node.end_lineno, node.end_col_offset)
        out.append(
            Literal(file, text, tuple([start] * max(len(text) - 1, 0) + [end - 1]))
        )
    return out


# --- sentences --------------------------------------------------------------


def _strip_tags(lit: Literal) -> tuple[str, tuple[int, ...]]:
    """Replace each tag by a newline, keeping one source offset per character."""

    if "<" not in lit.text:
        return lit.text, lit.offsets
    chars: list[str] = []
    offs: list[int] = []
    pos = 0
    for match in _TAG.finditer(lit.text):
        chars.extend(lit.text[pos : match.start()])
        offs.extend(lit.offsets[pos : match.start()])
        chars.append("\n")
        offs.append(lit.offsets[match.start()])
        pos = match.end()
    chars.extend(lit.text[pos:])
    offs.extend(lit.offsets[pos:])
    return "".join(chars), tuple(offs)


def _sentences(lit: Literal) -> list[Sentence]:
    text, offsets = _strip_tags(lit)
    pieces: list[tuple[int, str]] = []
    pos = 0
    for match in _SENTENCE_END.finditer(text):
        keep = 1 if text[match.start()] in ".;!?" else 0
        pieces.append((pos, text[pos : match.start() + keep]))
        pos = match.end()
    pieces.append((pos, text[pos:]))
    found: list[Sentence] = []
    for at, raw in pieces:
        sentence = normalise(html.unescape(raw))
        if len(_WORDS.findall(sentence)) < 5 or _CODEISH.search(sentence):
            continue
        kinds = tuple(k for k, pat in PATTERNS.items() if pat.search(sentence))
        if not kinds or not raw or at >= len(offsets):
            continue
        start = offsets[at]
        end = offsets[min(at + len(raw), len(offsets)) - 1] + 1
        found.append(Sentence(lit.file, start, end, sentence, kinds))
    return found


def scan_text(file: str, source: str) -> list[Sentence]:
    if file.endswith(".js"):
        literals = js_literals(file, source)
    elif file.endswith(".html"):
        literals = html_literals(file, source)
    elif file.endswith(".py"):
        literals = py_literals(file, source)
    else:
        raise ValueError(f"cannot scan {file}")
    seen: dict[str, Sentence] = {}
    for lit in literals:
        for sentence in _sentences(lit):
            seen.setdefault(sentence.key, sentence)
    return list(seen.values())


def scan_repo(repo: Path, files: tuple[str, ...] = SCAN_FILES) -> list[Sentence]:
    found: list[Sentence] = []
    for rel in files:
        path = repo / rel
        if path.is_file():
            found.extend(scan_text(rel, path.read_bytes().decode("utf-8")))
    return found
